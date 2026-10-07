import { describe, expect, it } from "vitest";

import { ApiError } from "../../lib/api";
import {
  answerMatchesSelection,
  followErrorMessage,
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

describe("Explore follow errors", () => {
  it("explains a missing or private repository", () => {
    expect(followErrorMessage(new ApiError(404, "Repository not found"))).toBe(
      "Repository not found — or it is private and unavailable to the GitHub App.",
    );
  });

  it("asks for a retry when the GitHub check fails", () => {
    expect(followErrorMessage(new ApiError(502, "Github access check failed"))).toBe(
      "GitHub check failed — try again.",
    );
  });

  it("states the wait when the indexing limit is reached", () => {
    expect(followErrorMessage(new ApiError(429, "Rate limit", 1800))).toBe(
      "You've queued as many new repositories for indexing as you can for now. Try again in about 30 minutes.",
    );
    expect(followErrorMessage(new ApiError(429, "Rate limit", 61))).toContain(
      "Try again in about 2 minutes.",
    );
    expect(followErrorMessage(new ApiError(429, "Rate limit", 30))).toContain(
      "Try again in about a minute.",
    );
  });

  it("falls back to later when the wait is unknown", () => {
    expect(followErrorMessage(new ApiError(429, "Rate limit"))).toContain(
      "Try again later.",
    );
  });

  it("uses a generic message for anything else", () => {
    expect(followErrorMessage(new ApiError(500, "Database error"))).toBe(
      "Could not add that repository.",
    );
    expect(followErrorMessage(new TypeError("Failed to fetch"))).toBe(
      "Could not add that repository.",
    );
  });
});
