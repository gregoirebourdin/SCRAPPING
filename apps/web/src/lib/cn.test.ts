import { cn } from "@scout/design-system";
import { describe, expect, it } from "vitest";

describe("cn", () => {
  it("keeps the text colour when a type-scale size follows it", () => {
    // Regression: tailwind-merge took `text-body` for a colour and dropped `text-accent-contrast`.
    expect(cn("bg-accent text-accent-contrast", "h-7 px-2.5 text-body")).toBe("bg-accent text-accent-contrast h-7 px-2.5 text-body");
  });

  it("still resolves real conflicts", () => {
    expect(cn("text-body text-fg-3", "text-meta")).toBe("text-fg-3 text-meta");
    expect(cn("text-fg", "text-danger")).toBe("text-danger");
  });
});
