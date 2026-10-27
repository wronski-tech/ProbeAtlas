import { describe, expect, it } from "vitest";
import { describeTarget, formatInterval, formatMs, formatPct, query, rangeToSince, relativeTime } from "./format";

describe("formatMs", () => {
  it("renders missing values as a dash rather than zero", () => {
    expect(formatMs(null)).toBe("—");
    expect(formatMs(undefined)).toBe("—");
    expect(formatMs(0)).toBe("0.0 ms");
  });
  it("scales units", () => {
    expect(formatMs(12.345)).toBe("12.3 ms");
    expect(formatMs(250.4)).toBe("250 ms");
    expect(formatMs(1234)).toBe("1.23 s");
    expect(formatMs(15000)).toBe("15.0 s");
  });
});

describe("formatPct", () => {
  it("handles ratios and percentages", () => {
    expect(formatPct(0.9876, { ratio: true })).toBe("98.76%");
    expect(formatPct(1, { ratio: true })).toBe("100%");
    expect(formatPct(99.5)).toBe("99.50%");
    expect(formatPct(null)).toBe("—");
  });
});

describe("relativeTime", () => {
  const now = new Date("2026-10-27T12:00:00Z");
  it("formats past and future", () => {
    expect(relativeTime("2026-10-27T11:59:30Z", now)).toBe("30s ago");
    expect(relativeTime("2026-10-27T10:00:00Z", now)).toBe("2h ago");
    expect(relativeTime("2026-10-27T12:10:00Z", now)).toBe("10m from now");
    expect(relativeTime(null, now)).toBe("never");
  });
});

describe("query", () => {
  it("drops empty values and encodes", () => {
    expect(query({ a: "x y", b: "", c: null, d: 0, e: false })).toBe("?a=x+y&d=0&e=false");
    expect(query({})).toBe("");
  });
});

describe("misc", () => {
  it("formats intervals", () => {
    expect(formatInterval(30)).toBe("30s");
    expect(formatInterval(120)).toBe("2m");
    expect(formatInterval(7200)).toBe("2h");
  });
  it("computes range start", () => {
    expect(rangeToSince("1h", new Date("2026-10-27T12:00:00Z"))).toBe("2026-10-27T11:00:00.000Z");
  });
  it("describes targets", () => {
    expect(describeTarget({ type: "tcp", target: "db.example.com", settings: { port: 5432 } })).toBe("db.example.com:5432");
    expect(describeTarget({ type: "dns", target: "example.com", settings: {} })).toBe("example.com (A)");
  });
});
