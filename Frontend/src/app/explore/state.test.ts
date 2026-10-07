import { describe, expect, it } from "vitest";

import {
  answerMatchesSelection,
  repositorySelectionChanged,
} from "./state";

describe("Explore repository selection", () => {
  it("preserves a deep-linked question during the initial selection", () => {
    expect(repositorySelectionChanged(undefined, "org/one")).toBe(false);
  });

  it("resets exploration state when the repository changes", () => {
    expect(repositorySelectionChanged("org/one", "org/two")).toBe(true);
    expect(repositorySelectionChanged("org/one", "org/one")).toBe(false);
  });

  it("only displays an answer for its repository and ref", () => {
    const answer = { repoName: "org/one", ref: "main" };

    expect(answerMatchesSelection(answer, "org/one", "main")).toBe(true);
    expect(answerMatchesSelection(answer, "org/two", "main")).toBe(false);
    expect(answerMatchesSelection(answer, "org/one", "develop")).toBe(false);
  });
});
