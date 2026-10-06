import { describe, expect, it } from "vitest";

import { parseIssueUrl } from "./issue-url";

describe("parseIssueUrl", () => {
  it("accepts a GitHub issue URL", () => {
    expect(parseIssueUrl("https://github.com/jballo/camino/issues/53")).toBe(
      "https://github.com/jballo/camino/issues/53",
    );
  });

  it("normalises whitespace, www, http, a trailing slash, query and hash", () => {
    expect(
      parseIssueUrl("  http://www.github.com/owner/repo.js/issues/7/?x=1#issuecomment-2 "),
    ).toBe("https://github.com/owner/repo.js/issues/7");
  });

  it.each([
    undefined,
    null,
    "",
    "not a url",
    "github.com/owner/repo/issues/1",
    "https://github.com/owner/repo/pull/1",
    "https://github.com/owner/repo/issues",
    "https://github.com/owner/repo/issues/0",
    "https://github.com/owner/repo/issues/12/comments",
    "https://gitlab.com/owner/repo/issues/1",
    "https://github.com.evil.example/owner/repo/issues/1",
    "https://user@github.com/owner/repo/issues/1",
    "https://github.com:8443/owner/repo/issues/1",
    "https://github.com/owner/../issues/1",
    "javascript:alert(1)//github.com/o/r/issues/1",
  ])("rejects %j", (value) => {
    expect(parseIssueUrl(value)).toBeNull();
  });
});
