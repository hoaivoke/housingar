// Drives the built site against the mock API at laptop and phone sizes and saves screenshots.
//   node mock/server.mjs --port 8790 --dist dist &   then   node e2e/run.mjs http://127.0.0.1:8790 out/
import { createRequire } from "node:module";
const { chromium } = createRequire(import.meta.url)("playwright");
import fs from "node:fs";

const base = process.argv[2] ?? "http://127.0.0.1:8790";
const out = process.argv[3] ?? "e2e-out";
fs.mkdirSync(out, { recursive: true });
const problems = [];
const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });

async function session(name, viewport, mobile) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: mobile ? 2 : 1, isMobile: mobile, hasTouch: mobile });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") problems.push(`${name} console: ${m.text()}`); });
  page.on("pageerror", (e) => problems.push(`${name} page error: ${e.message}`));
  const shot = (n) => page.screenshot({ path: `${out}/${name}-${n}.png` });
  const step = (t) => console.log(`${name}: ${t}`);

  await page.goto(base + "/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await shot("1-search-empty");
  await page.fill("#near", "Blacksburg, VA");
  await page.click("button[type=submit]");
  await page.waitForSelector(".result");
  const count = await page.locator(".result").count();
  step(`search → ${count} results`);
  await shot("2-results");
  await page.selectOption("#bedrooms", "2");
  await page.click("button[type=submit]");
  await page.waitForTimeout(400);
  const twoBed = await page.locator(".result-rooms").allTextContents();
  if (!twoBed.length || !twoBed.every((t) => t.startsWith("2 bedrooms"))) problems.push(`${name}: 2-bedroom filter gave ${JSON.stringify(twoBed)}`);
  await page.locator(".result").first().click();
  await page.waitForSelector(".listing-rent");
  await page.waitForSelector(".plan-card .plan");
  step(`listing: ${await page.locator("h1").textContent()}`);
  await shot("3-listing");
  await page.click("text=Plan and furnish in 3D");
  await page.waitForSelector(".room-canvas");
  await page.waitForTimeout(1500);
  await shot("4-room");

  const listingIdEarly = decodeURIComponent(page.url().split("/room/")[1]);
  // Put this listing back the way it was afterwards, so a run leaves nothing behind.
  const api = `${base}/v1/listings/${encodeURIComponent(listingIdEarly)}`;
  const originalFurniture = await (await fetch(`${api}/furniture`)).json();
  const originalLayout = await (await fetch(`${api}/layout`)).json();
  const before = originalFurniture.placements.length;
  // Add a chair from My stuff.
  await page.click("text=Add furniture");
  await page.waitForSelector(".drawer-item");
  await shot("5-drawer");
  await page.locator(".drawer-item").first().click();
  await page.waitForSelector(".selected-bar:not([hidden])", { timeout: 30000 });
  await page.waitForTimeout(800);
  step(`added: ${await page.locator(".selected-name").textContent()}`);
  await shot("6-added");
  await page.click("text=Turn left");
  await page.waitForTimeout(300);
  await page.click(".selected-actions >> text=Done");
  // Add a second one.
  await page.click("text=Add furniture");
  await page.locator(".drawer-item").nth(1).click();
  await page.waitForSelector(".selected-bar:not([hidden])", { timeout: 30000 });
  await page.waitForTimeout(1200);
  await shot("7-two");

  const listingId = decodeURIComponent(page.url().split("/room/")[1]);
  const furniture = await (await fetch(`${base}/v1/listings/${encodeURIComponent(listingId)}/furniture`)).json();
  step(`saved furniture: ${furniture.placements.map((p) => `${p.name} @ ${p.position.map((v) => v.toFixed(2)).join(",")} yaw ${p.yaw_deg}`).join("; ")}`);
  if (furniture.placements.length !== before + 2) problems.push(`${name}: expected ${before + 2} saved placements, got ${furniture.placements.length}`);

  // Plan: select a bedroom, type a new width, drag a wall.
  if (mobile) await page.click(".view-tabs >> text=Plan");
  await page.locator('.floor[data-room="bed_1"]').first().click({ force: true });
  await page.waitForSelector("#room-width");
  const widthBefore = await page.inputValue("#room-width");
  await page.fill("#room-width", "13' 6\"");
  await page.press("#room-width", "Enter");
  await page.waitForTimeout(1200);
  const after = await page.inputValue("#room-width");
  step(`bedroom width ${widthBefore} → ${after}`);
  if (after !== "13′ 6″") problems.push(`${name}: width shows ${after}`);
  const hit = page.locator('.handle-hit[data-wall="bottom"]');
  const box = await hit.boundingBox();
  if (box) {
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x, y + 25, { steps: 5 });
    await page.mouse.up();
  }
  await page.waitForTimeout(1300);
  const depth = await page.inputValue("#room-depth");
  step(`after dragging the bottom wall, depth ${depth}; status "${await page.textContent(".save-status")}"`);
  await shot("8-plan-edit");
  const saved = await (await fetch(`${base}/v1/listings/${encodeURIComponent(listingId)}/layout`)).json();
  step(`saved layout: estimated=${saved.estimated}, source=${saved.source}, area ${saved.total_area_m2}`);
  if (saved.estimated !== false) problems.push(`${name}: layout not saved as edited`);

  // Walk through.
  if (mobile) await page.click(".view-tabs >> text=3D");
  await page.click("text=Walk through");
  await page.waitForTimeout(800);
  await shot("9-walk");
  if (!mobile) {
    await page.locator(".room-canvas").focus();
    await page.keyboard.down("w");
    await page.waitForTimeout(900);
    await page.keyboard.up("w");
  } else {
    const fwd = page.locator('.pad[aria-label="Walk forward"]');
    const b = await fwd.boundingBox();
    await page.touchscreen.tap(b.x + b.width / 2, b.y + b.height / 2);
  }
  await page.waitForTimeout(500);
  await shot("10-walked");

  if (mobile) {
    // Look around by moving the phone: simulated orientation readings turn the view.
    const fire = (alpha, beta, gamma) => page.evaluate(([a, b, g]) => {
      window.dispatchEvent(new DeviceOrientationEvent("deviceorientation", { alpha: a, beta: b, gamma: g }));
    }, [alpha, beta, gamma]);
    const yawNow = () => page.evaluate(() => {
      const c = window.__roomScene.camera; const v = { x: 0, y: 0, z: -1 };
      const q = c.quaternion; // rotate (0,0,-1) by q
      const ix = q.w * v.x + q.y * v.z - q.z * v.y, iy = q.w * v.y + q.z * v.x - q.x * v.z, iz = q.w * v.z + q.x * v.y - q.y * v.x, iw = -q.x * v.x - q.y * v.y - q.z * v.z;
      const x = ix * q.w + iw * -q.x + iy * -q.z - iz * -q.y, z = iz * q.w + iw * -q.z + ix * -q.y - iy * -q.x;
      return Math.atan2(-x, -z) * 180 / Math.PI;
    });
    const timer = setInterval(() => fire(0, 90, 0).catch(() => {}), 100);
    await page.click("text=Look around");
    await page.waitForTimeout(1200);
    const y0 = await yawNow();
    clearInterval(timer);
    for (let i = 0; i < 5; i++) { await fire(90, 90, 0); await page.waitForTimeout(60); }
    const y1 = await yawNow();
    step(`look around: yaw ${y0.toFixed(0)}° → ${y1.toFixed(0)}° after turning the phone 90°; button "${await page.textContent(".look")}"`);
    if (Math.abs(((y1 - y0 + 540) % 360) - 180) < 60) problems.push(`${name}: look-around didn't turn the view (${y0} → ${y1})`);
    await shot("10b-look");
    await page.click(".look");
  }

  // Reload: plan and furniture come back.
  await page.reload();
  await page.waitForSelector(".room-canvas");
  await page.waitForTimeout(2500);
  await shot("11-reloaded");
  await page.goto(base + "/#/stuff");
  await page.waitForSelector(".stuff");
  await shot("12-stuff");
  await ctx.close();
  const { updated: _u, ...furnitureBody } = originalFurniture;
  const put = (path, body) => fetch(`${api}/${path}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const restored = [await put("furniture", furnitureBody),
    originalLayout.estimated ? await fetch(`${api}/layout`, { method: "DELETE" }) : await put("layout", originalLayout)];
  if (restored.some((r) => !r.ok)) problems.push(`${name}: couldn't put the listing back (${restored.map((r) => r.status).join(", ")})`);
}

await session("laptop", { width: 1366, height: 860 }, false);
await session("phone", { width: 412, height: 915 }, true);
await browser.close();
console.log(problems.length ? "PROBLEMS:\n" + problems.join("\n") : "no problems");
