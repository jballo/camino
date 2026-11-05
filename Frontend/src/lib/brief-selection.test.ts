import { describe, expect, it } from "vitest";

import type { BriefSummary } from "../types/brief";
import { filterBriefs, nextSelection, resolveSelection } from "./brief-selection";

function brief(id: number, issueTitle: string, issueNumber = id): BriefSummary {
  return {
    id,
    status: "complete",
    repoName: "jballo/camino",
    issueRepo: "jballo/camino",
    ref: "dev",
    issueNumber,
    issueTitle,
    createdAt: "2026-11-05T00:00:00Z",
  };
}

const A = brief(1, "Allow users to delete issue briefs", 53);
const B = brief(2, "Brief pane shows only the first warning", 82);
const C = brief(3, "Long target-branch names are cut off", 83);

describe("filterBriefs", () => {
  it("returns every brief for a blank query", () => {
    expect(filterBriefs([A, B, C], "  ")).toEqual([A, B, C]);
  });

  it("matches title, repository and issue number, ignoring case", () => {
    expect(filterBriefs([A, B, C], "DELETE")).toEqual([A]);
    expect(filterBriefs([A, B, C], "#82")).toEqual([B]);
    expect(filterBriefs([A, B, C], "jballo/camino #83")).toEqual([C]);
    expect(filterBriefs([A, B, C], "nothing like this")).toEqual([]);
  });
});

describe("nextSelection", () => {
  it("selects the next brief after the first one", () => {
    expect(nextSelection([A, B, C], A.id)).toBe(B.id);
  });

  it("selects the next brief after a middle one", () => {
    expect(nextSelection([A, B, C], B.id)).toBe(C.id);
  });

  it("selects the previous brief after the last one", () => {
    expect(nextSelection([A, B, C], C.id)).toBe(B.id);
  });

  it("only picks briefs the filter shows", () => {
    // The filter shows A and C; B is hidden, so deleting A skips past it.
    expect(nextSelection([A, C], A.id)).toBe(C.id);
  });

  it("selects nothing when the deleted brief was the only one shown", () => {
    expect(nextSelection([A], A.id)).toBeUndefined();
    expect(nextSelection([], A.id)).toBeUndefined();
  });

  it("selects the first shown brief when the deleted one was hidden", () => {
    expect(nextSelection([B, C], A.id)).toBe(B.id);
  });

  it("never picks a brief whose delete finished before the list re-rendered", () => {
    // The filter shows A and B. Deleting A and then B both finish before
    // React re-renders, so the visible list still holds A when B's delete
    // picks what to select next. A is already deleted, so nothing is left.
    const deleted = new Set([A.id, B.id]);
    expect(nextSelection([A, B], B.id, deleted)).toBeUndefined();
    expect(nextSelection([A, B, C], B.id, deleted)).toBe(C.id);
    expect(nextSelection([B, A, C], B.id, deleted)).toBe(C.id);
  });
});

describe("resolveSelection", () => {
  it("keeps the current selection", () => {
    expect(resolveSelection(B.id, [A, B, C], false)).toBe(B.id);
    expect(resolveSelection(B.id, [A, B, C], true)).toBe(B.id);
  });

  it("selects the newest brief on first load", () => {
    expect(resolveSelection(undefined, [A, B, C], false)).toBe(A.id);
    expect(resolveSelection(undefined, [], false)).toBeUndefined();
  });

  it("keeps a selection cleared by a delete when a late list arrives", () => {
    // The filter showed only A. A is deleted, so nothing is selected; then a
    // list request that started earlier resolves, filtered of A. B is hidden
    // by the filter and must not be selected behind the user's back.
    const deleted = new Set([A.id]);
    const lateResponse = [A, B, C].filter((item) => !deleted.has(item.id));
    expect(resolveSelection(undefined, lateResponse, true)).toBeUndefined();
  });
});
