import { describe, expect, it } from "vitest";

import { safeRedirectPath } from "./safe-redirect";

describe("safeRedirectPath", () => {
  it("keeps same-origin paths with their query", () => {
    expect(safeRedirectPath("/briefs/12", "/briefs")).toBe("/briefs/12");
    expect(safeRedirectPath("/briefs?issue=x", "/briefs")).toBe("/briefs?issue=x");
  });

  it("uses the first value of a repeated parameter", () => {
    expect(safeRedirectPath(["/tours", "/settings"], "/briefs")).toBe("/tours");
  });

  it.each([
    undefined,
    null,
    "",
    "briefs",
    "https://evil.example/",
    "//evil.example",
    "/\\evil.example",
    "/briefs\n",
  ])("falls back for %j", (value) => {
    expect(safeRedirectPath(value, "/briefs")).toBe("/briefs");
  });
});
