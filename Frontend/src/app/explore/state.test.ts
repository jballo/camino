import { describe, expect, it } from "vitest";

import { ApiError } from "../../lib/api";
import {
  activeIngestKey,
  answerMatchesSelection,
  applyIngestUpdate,
  followErrorMessage,
  ingestMetaLabel,
  ingestPanelStatus,
  ingestsFromOverview,
  isIngestActive,
  jobsFromActiveIngestKey,
  requestIndexLabel,
  repositorySelectionChanged,
  type RepoIngest,
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

function ingest(overrides: Partial<RepoIngest> = {}): RepoIngest {
  return { id: 7, status: "pending", ref: "main", error: null, ...overrides };
}

describe("Explore index requests", () => {
  it("treats only pending and running requests as in flight", () => {
    expect(isIngestActive(ingest({ status: "pending" }))).toBe(true);
    expect(isIngestActive(ingest({ status: "running" }))).toBe(true);
    expect(isIngestActive(ingest({ status: "complete" }))).toBe(false);
    expect(isIngestActive(ingest({ status: "failed" }))).toBe(false);
    expect(isIngestActive(ingest({ status: "cancelled" }))).toBe(false);
    expect(isIngestActive(undefined)).toBe(false);
  });

  it("collects the overview's requests by repository", () => {
    expect(
      ingestsFromOverview([
        { repoName: "org/a", ingest: ingest({ id: 1 }) },
        { repoName: "org/b", ingest: null },
        { repoName: "org/c" },
      ]),
    ).toEqual({ "org/a": ingest({ id: 1 }) });
  });

  it("keys polling on the in-flight jobs, not on their status", () => {
    const pending = {
      "org/b": ingest({ id: 2 }),
      "org/a": ingest({ id: 1 }),
      "org/c": ingest({ id: 3, status: "failed" }),
    };
    const running = { ...pending, "org/a": ingest({ id: 1, status: "running" }) };

    expect(activeIngestKey(pending)).toBe("org/a#1,org/b#2");
    expect(activeIngestKey(running)).toBe(activeIngestKey(pending));
    expect(activeIngestKey({})).toBe("");
  });

  it("reads the in-flight jobs back from their key", () => {
    expect(jobsFromActiveIngestKey("org/a#1,org.x/b-c_d#22")).toEqual([
      { repoName: "org/a", id: 1 },
      { repoName: "org.x/b-c_d", id: 22 },
    ]);
    expect(jobsFromActiveIngestKey("")).toEqual([]);
  });

  it("applies a polled status to the request it belongs to", () => {
    const ingests = { "org/a": ingest({ id: 1 }) };

    expect(
      applyIngestUpdate(ingests, {
        id: 1,
        repoName: "org/a",
        status: "cancelled",
        error: "GitHub installation suspended",
      }),
    ).toEqual({
      "org/a": ingest({
        id: 1,
        status: "cancelled",
        error: "GitHub installation suspended",
      }),
    });
  });

  it("ignores a poll for a request that a newer one replaced", () => {
    const ingests = { "org/a": ingest({ id: 2 }) };

    expect(
      applyIngestUpdate(ingests, {
        id: 1,
        repoName: "org/a",
        status: "failed",
        error: "boom",
      }),
    ).toBe(ingests);
  });

  it("labels each request state in the repository list", () => {
    expect(ingestMetaLabel(ingest({ status: "pending" }))).toBe("index queued");
    expect(ingestMetaLabel(ingest({ status: "running" }))).toBe("indexing…");
    expect(ingestMetaLabel(ingest({ status: "failed" }))).toBe("index failed");
    expect(ingestMetaLabel(ingest({ status: "cancelled" }))).toBe(
      "request cancelled",
    );
    expect(ingestMetaLabel(undefined)).toBeUndefined();
  });

  it("shows the reason a request failed or was cancelled", () => {
    expect(
      ingestPanelStatus(
        ingest({ status: "cancelled", error: "GitHub installation suspended" }),
      ),
    ).toEqual({
      tone: "problem",
      message: "Your index request was cancelled.",
      detail: "GitHub installation suspended",
    });
    expect(
      ingestPanelStatus(ingest({ status: "failed", error: null })),
    ).toEqual({ tone: "problem", message: "Indexing failed.", detail: undefined });
    expect(ingestPanelStatus(ingest({ status: "running" }))?.tone).toBe(
      "progress",
    );
    expect(ingestPanelStatus(undefined)).toBeUndefined();
  });

  it("offers a retry only once a request has ended badly", () => {
    expect(requestIndexLabel(undefined)).toBe("Request index");
    expect(requestIndexLabel(ingest({ status: "pending" }))).toBe("Queued");
    expect(requestIndexLabel(ingest({ status: "running" }))).toBe("Indexing");
    expect(requestIndexLabel(ingest({ status: "failed" }))).toBe(
      "Request again",
    );
    expect(requestIndexLabel(ingest({ status: "cancelled" }))).toBe(
      "Request again",
    );
  });
});
