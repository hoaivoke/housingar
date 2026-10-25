import { h, toast } from "./dom";

/** Native ARCore receives the same-origin saved plan, never an inline unvalidated model. */
export function roomArUrl(id: string, origin: string, roomId?: string): string {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(id) || (roomId != null && !/^[A-Za-z0-9_-]{1,64}$/.test(roomId))) throw new Error("Invalid room ID");
  const service = new URL(origin);
  if (!["http:", "https:"].includes(service.protocol) || service.username || service.password || service.pathname !== "/" || service.search || service.hash) throw new Error("Invalid Mac address");
  const query = new URLSearchParams({ service: service.origin });
  if (roomId) query.set("room_id", roomId);
  return `housingar://room/${id}?${query}`;
}

export function roomArIntent(id: string, origin: string, roomId?: string): string {
  const link = roomArUrl(id, origin, roomId);
  // Explicit package prevents another app from accidentally intercepting the preview.
  return `intent://${link.slice("housingar://".length)}#Intent;scheme=housingar;package=com.housingar.inspect;end`;
}

export function arButton(id: string, roomId?: string, beforeOpen?: () => boolean): HTMLElement {
  const android = /Android/i.test(navigator.userAgent);
  return h("a.button.secondary", {
    href: android ? roomArIntent(id, location.origin, roomId) : roomArUrl(id, location.origin, roomId),
    onclick: (event: MouseEvent) => {
      if (!android) {
        event.preventDefault();
        alert("Open this website on your Pixel in Chrome, then tap View in AR. Install the Housing AR Android app first. The phone must be connected to the Mac through USB or the same Wi-Fi network. AR uses the phone camera; the Mac keeps its 3D view.");
      } else if (beforeOpen && !beforeOpen()) {
        event.preventDefault();
        toast("Wait for your plan and furniture to finish saving, then tap View in AR again.", "error");
      }
    },
  }, roomId ? "View this room in AR" : "View in AR");
}
