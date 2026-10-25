import { describe, it, expect } from "vitest";
import { roomArUrl, roomArIntent } from "../src/ar";

describe("native room AR handoff", () => {
  it("preserves the service origin and selected room", () => {
    const u = new URL(roomArUrl("sample_01", "http://192.168.1.8:8000", "bedroom_2"));
    expect(u.protocol).toBe("housingar:");
    expect(u.host).toBe("room");
    expect(u.pathname).toBe("/sample_01");
    expect(u.searchParams.get("service")).toBe("http://192.168.1.8:8000");
    expect(u.searchParams.get("room_id")).toBe("bedroom_2");
  });
  it("supports the USB origin and targets the Android package", () => {
    expect(roomArIntent("test", "http://127.0.0.1:8000")).toBe("intent://room/test?service=http%3A%2F%2F127.0.0.1%3A8000#Intent;scheme=housingar;package=com.housingar.inspect;end");
  });
  it("rejects unsafe IDs and service addresses", () => {
    for (const id of ["../object", "bad;end", "", "x".repeat(65)]) expect(() => roomArUrl(id, "http://localhost:8000")).toThrow();
    for (const origin of ["javascript:alert(1)", "https://a.com/path", "https://a.com/?token=x", "http://user:pass@a.com"]) expect(() => roomArUrl("test", origin)).toThrow();
    expect(() => roomArUrl("test", "http://localhost:8000", "../x")).toThrow();
  });
});
