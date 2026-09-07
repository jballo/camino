import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  enqueueRepositoryIngestion,
  IngestionTimeoutError,
  isAbortError,
  pollRepositoryIngestion,
} from "./repository-ingestion";
import type { RepositoryIngestionJob } from "../types/repository-ingestion";

const BACKEND_URL = "http://127.0.0.1:8000";

function mockResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

function job(
  status: RepositoryIngestionJob["status"],
  overrides: Partial<RepositoryIngestionJob> = {},
): RepositoryIngestionJob {
  return {
    id: 42,
    status,
    repoName: "camino/app",
    attempts: status === "pending" ? 0 : 1,
    result: null,
    error: null,
    ...overrides,
  };
}

describe("repository ingestion jobs", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("enqueues an ingestion job for the selected repository", async () => {
    fetchMock.mockResolvedValue(mockResponse({ id: 42, status: "pending" }));

    await expect(
      enqueueRepositoryIngestion("camino/app", "my-token"),
    ).resolves.toEqual({ id: 42, status: "pending" });

    expect(fetchMock).toHaveBeenCalledWith(
      `${BACKEND_URL}/api/v1/repositories/ingest`,
      {
        method: "POST",
        headers: {
          Authorization: "Bearer my-token",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ repoName: "camino/app" }),
      },
    );
  });

  it("polls through queued and running updates until completion", async () => {
    const pending = job("pending");
    const running = job("running");
    const complete = job("complete", {
      result: { chunks_inserted: 12, embeddings_created: 12 },
    });
    fetchMock
      .mockResolvedValueOnce(mockResponse(pending))
      .mockResolvedValueOnce(mockResponse(running))
      .mockResolvedValueOnce(mockResponse(complete));
    const getToken = vi.fn().mockResolvedValue("my-token");
    const onUpdate = vi.fn();

    const result = await pollRepositoryIngestion(42, getToken, {
      intervalMs: 0,
      onUpdate,
    });

    expect(result).toEqual(complete);
    expect(onUpdate.mock.calls.map(([update]) => update.status)).toEqual([
      "pending",
      "running",
      "complete",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(getToken).toHaveBeenCalledTimes(3);
    expect(fetchMock).toHaveBeenLastCalledWith(
      `${BACKEND_URL}/api/v1/repositories/ingest/42`,
      {
        method: "GET",
        headers: { Authorization: "Bearer my-token" },
        body: undefined,
      },
    );
  });

  it("stops polling when the job fails", async () => {
    const failed = job("failed", {
      attempts: 3,
      error: "Exceeded max attempts: GitHub unavailable",
    });
    fetchMock.mockResolvedValue(mockResponse(failed));

    await expect(
      pollRepositoryIngestion(
        42,
        vi.fn().mockResolvedValue("my-token"),
        { intervalMs: 0 },
      ),
    ).resolves.toEqual(failed);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("times out when a job never reaches a terminal state", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(mockResponse(job("pending")));
    const polling = pollRepositoryIngestion(
      42,
      vi.fn().mockResolvedValue("my-token"),
      { intervalMs: 100, timeoutMs: 250 },
    );
    const rejection = expect(polling).rejects.toBeInstanceOf(
      IngestionTimeoutError,
    );

    await vi.advanceTimersByTimeAsync(250);

    await rejection;
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("stops polling when the caller aborts", async () => {
    const controller = new AbortController();
    fetchMock.mockResolvedValue(mockResponse(job("pending")));

    const polling = pollRepositoryIngestion(
      42,
      vi.fn().mockResolvedValue("my-token"),
      {
        intervalMs: 1000,
        signal: controller.signal,
        onUpdate: () => controller.abort(),
      },
    );

    await expect(polling).rejects.toSatisfy(isAbortError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
