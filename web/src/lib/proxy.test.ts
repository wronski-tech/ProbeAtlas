import { describe, expect, it } from "vitest";
import { clientIpFromForwardedFor, proxiedApiPath } from "./proxy";

describe("proxiedApiPath", () => {
  it("passes ordinary /v1 paths through", () => {
    expect(proxiedApiPath(["v1", "monitors", "0b1c2d3e-0000-4000-8000-000000000000", "regional-performance"])).toBe(
      "/v1/monitors/0b1c2d3e-0000-4000-8000-000000000000/regional-performance",
    );
  });

  it("rejects paths outside /v1", () => {
    expect(proxiedApiPath(["metrics"])).toBeNull();
    expect(proxiedApiPath(["agent", "v1", "jobs", "claim"])).toBeNull();
  });

  it("rejects decoded traversal segments that would be normalized to another route", () => {
    expect(proxiedApiPath(["v1", "../metrics"])).toBeNull(); // from /api/proxy/v1/..%2Fmetrics
    expect(proxiedApiPath(["v1", ".."])).toBeNull();
    expect(proxiedApiPath(["v1", "..\\metrics"])).toBeNull();
    expect(proxiedApiPath(["v1", ""])).toBeNull();
    expect(proxiedApiPath(["v1", "monitors?x=1"])).toBeNull();
  });
});

describe("clientIpFromForwardedFor", () => {
  it("trusts nothing without proxies, because Next.js keeps client-supplied headers", () => {
    expect(clientIpFromForwardedFor("203.0.113.7", 0)).toBeNull();
  });

  it("takes the entry appended by the outermost trusted proxy, ignoring forged entries to its left", () => {
    expect(clientIpFromForwardedFor("203.0.113.7", 1)).toBe("203.0.113.7");
    expect(clientIpFromForwardedFor("6.6.6.6, 203.0.113.7", 1)).toBe("203.0.113.7");
    expect(clientIpFromForwardedFor("6.6.6.6, 203.0.113.7, 10.0.0.2", 2)).toBe("203.0.113.7");
    expect(clientIpFromForwardedFor("2001:db8::1", 1)).toBe("2001:db8::1");
  });

  it("returns null when the header is shorter than the proxy chain or not an IP address", () => {
    expect(clientIpFromForwardedFor("203.0.113.7", 2)).toBeNull();
    expect(clientIpFromForwardedFor(null, 1)).toBeNull();
    expect(clientIpFromForwardedFor("evil\r\nX-Injected: 1", 1)).toBeNull();
  });
});
