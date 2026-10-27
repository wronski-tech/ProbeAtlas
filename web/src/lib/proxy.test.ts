import { describe, expect, it } from "vitest";
import { proxiedApiPath } from "./proxy";

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
