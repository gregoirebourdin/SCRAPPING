import { describe, expect, it } from "vitest";

import { employees, freshness, hostname, n, pct } from "./format";

describe("format", () => {
  it("formats numbers and percentages", () => {
    expect(n(1234567)).toBe("1,234,567");
    expect(n(null)).toBe("—");
    expect(pct(0.8567, 1)).toBe("85.7%");
  });

  it("formats employee ranges", () => {
    expect(employees(2, 30)).toBe("2–30");
    expect(employees(10, 10)).toBe("10");
    expect(employees(50, null)).toBe("50+");
    expect(employees(null, null)).toBe("—");
  });

  it("extracts hostnames safely", () => {
    expect(hostname("https://www.agence-lumiere.fr/equipe")).toBe("agence-lumiere.fr");
    expect(hostname("not a url")).toBe("not a url");
  });

  it("buckets freshness", () => {
    expect(freshness(new Date().toISOString())).toBe("fresh");
    expect(freshness(new Date(Date.now() - 40 * 86400000).toISOString())).toBe("90d");
    expect(freshness(new Date(Date.now() - 200 * 86400000).toISOString())).toBe("stale");
  });
});
