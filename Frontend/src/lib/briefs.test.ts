import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import {
  REPOSITORY_INGEST_LIMIT_DETAIL,
  briefCreateErrorMessage,
  createIssueBrief,
  getIssueBrief,
  pollIssueBrief,
  previewIssueBrief,
} from "./briefs";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("issue brief client", () => {
  it("previews an issue with an optional branch override", async () => {
    const payload = { title: "Fix it" };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue(payload),
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      previewIssueBrief(
        "https://github.com/acme/app/issues/4",
        vi.fn().mockResolvedValue("token"),
        "develop",
      ),
    ).resolves.toEqual(payload);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      issueUrl: "https://github.com/acme/app/issues/4",
      targetBranch: "develop",
    });
  });

  it("creates and reads a brief", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ id: 9, status: "pending" }) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ id: 9, status: "complete" }) });
    vi.stubGlobal("fetch", fetchMock);
    const token = vi.fn().mockResolvedValue("token");
    await createIssueBrief("https://github.com/acme/app/issues/4", "main", token);
    await getIssueBrief(9, token);
    expect(fetchMock.mock.calls[1][0]).toContain("/api/v1/briefs/9");
  });

  it("does not fetch without a token", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(getIssueBrief(1, vi.fn().mockResolvedValue(null))).rejects.toEqual(
      new ApiError(401, "Not authenticated"),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("polls a brief until it reaches a terminal status", async () => {
    const pending = { id: 9, status: "pending", phase: "queued" };
    const generating = { id: 9, status: "running", phase: "generating" };
    const complete = { id: 9, status: "complete", phase: "complete" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue(pending),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue(generating),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue(complete),
      });
    vi.stubGlobal("fetch", fetchMock);
    const getToken = vi.fn().mockResolvedValue("token");
    const onUpdate = vi.fn();

    await expect(
      pollIssueBrief(9, getToken, { intervalMs: 0, onUpdate }),
    ).resolves.toEqual(complete);

    expect(onUpdate.mock.calls.map(([brief]) => brief.status)).toEqual([
      "pending",
      "running",
      "complete",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(getToken).toHaveBeenCalledTimes(3);
  });

  it("keeps polling beyond ten minutes when no timeout is requested", async () => {
    vi.useFakeTimers();
    const pending = { id: 9, status: "pending", phase: "queued" };
    const complete = { id: 9, status: "complete", phase: "complete" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue(pending),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue(complete),
      });
    vi.stubGlobal("fetch", fetchMock);

    const polling = pollIssueBrief(
      9,
      vi.fn().mockResolvedValue("token"),
      { intervalMs: 10 * 60 * 1000 + 1 },
    );

    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 1);

    await expect(polling).resolves.toEqual(complete);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("brief creation error message", () => {
  it("names the indexing limit and the wait when a new ingest is refused", () => {
    const error = new ApiError(429, REPOSITORY_INGEST_LIMIT_DETAIL, 1800);
    expect(briefCreateErrorMessage(error, "fallback")).toBe(
      "This repository isn't indexed yet, and you've queued as many new repositories for indexing as you can for now. Try again in about 30 minutes.",
    );
  });

  it("keeps the brief limit wording for the brief bucket", () => {
    const error = new ApiError(429, "Rate limit exceeded. Try again later.", 90);
    expect(briefCreateErrorMessage(error, "fallback")).toBe(
      "Too many brief requests right now. Try again in about 2 minutes.",
    );
  });

  it("says later when the wait is unknown", () => {
    const error = new ApiError(429, "Rate limit exceeded. Try again later.");
    expect(briefCreateErrorMessage(error, "fallback")).toBe(
      "Too many brief requests right now. Try again later.",
    );
  });

  it("passes other errors through", () => {
    expect(briefCreateErrorMessage(new ApiError(502, "Github access check failed"), "fallback")).toBe(
      "Github access check failed",
    );
    expect(briefCreateErrorMessage("boom", "fallback")).toBe("fallback");
  });
});
