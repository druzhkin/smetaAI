import { describe, expect, it } from "vitest";

import { safeInternalPath } from "./navigation";

describe("same-origin navigation boundary", () => {
  it("accepts application paths", () => {
    expect(safeInternalPath("/projects/project-1/BOQ_SCOPE")).toBe(
      "/projects/project-1/BOQ_SCOPE",
    );
  });

  it.each([
    "https://attacker.example",
    "//attacker.example/path",
    "/\\attacker.example",
    "projects/project-1",
    "/projects/\u0000",
  ])("rejects an unsafe redirect target: %s", (target) => {
    expect(() => safeInternalPath(target)).toThrow("same-origin absolute path");
  });
});
