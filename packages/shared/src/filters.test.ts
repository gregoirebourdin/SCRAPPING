import { describe, expect, it } from "vitest";

import { completeFilters, isComplete } from "./filters";

describe("completeFilters", () => {
  it("returns null for empty groups", () => {
    expect(completeFilters(null)).toBeNull();
    expect(completeFilters({ op: "and", conditions: [] })).toBeNull();
  });

  it("drops conditions still being typed", () => {
    const g = completeFilters({
      op: "and",
      conditions: [
        { field: "email_status", operator: "eq", value: "SAFE" },
        { field: "title", operator: "contains", value: "" },
        { field: "icp_score", operator: "gte", value: null },
        { field: "email", operator: "is_empty" },
      ],
    });
    expect(g?.conditions?.map((c) => ("field" in c ? c.field : ""))).toEqual(["email_status", "email"]);
  });

  it("requires both bounds for between", () => {
    expect(isComplete({ field: "icp_score", operator: "between", value: [80, ""] })).toBe(false);
    expect(isComplete({ field: "icp_score", operator: "between", value: [80, 100] })).toBe(true);
  });

  it("requires a non-empty list for in", () => {
    expect(isComplete({ field: "email_status", operator: "in", value: [] })).toBe(false);
    expect(isComplete({ field: "email_status", operator: "in", value: ["SAFE"] })).toBe(true);
  });
});
