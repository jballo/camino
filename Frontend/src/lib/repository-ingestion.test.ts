import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  cancelRepositoryIngestion,
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

function mockErrorResponse(status: number, retryAfter?: string) {
  return {
    ok: false,
    status,
    json: vi.fn().mockResolvedValue({ detail: `Request failed (${status})` }),
    headers: {
      get: (name: string) =>
        name === "Retry-After" ? (retryAfter ?? null) : null,
    },
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
    ref: "main",
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
      enqueueRepositoryIngestion("camino/app", "feature/Issue44", "my-token"),
    ).resolves.toEqual({ id: 42, status: "pending" });

    expect(fetchMock).toHaveBeenCalledWith(
      `${BACKEND_URL}/api/v1/repositories/ingest`,
      {
        method: "POST",
        headers: {
          Authorization: "Bearer my-token",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          repoName: "camino/app",
          ref: "feature/Issue44",
        }),
      },
    );
  });

  it("cancels an ingestion job", async () => {
    const cancelled = job("cancelled");
    fetchMock.mockResolvedValue(mockResponse(cancelled));

    await expect(
      cancelRepositoryIngestion(42, "my-token"),
    ).resolves.toEqual(cancelled);

    expect(fetchMock).toHaveBeenCalledWith(
      `${BACKEND_URL}/api/v1/repositories/ingest/42/cancel`,
      {
        method: "POST",
        headers: { Authorization: "Bearer my-token" },
        body: undefined,
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

  it("stops polling when the job is cancelled", async () => {
    const cancelled = job("cancelled");
    fetchMock.mockResolvedValue(mockResponse(cancelled));

    await expect(
      pollRepositoryIngestion(
        42,
        vi.fn().mockResolvedValue("my-token"),
        { intervalMs: 0 },
      ),
    ).resolves.toEqual(cancelled);
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

  it("rejects on the first failed status request by default", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(
      pollRepositoryIngestion(
        42,
        vi.fn().mockResolvedValue("my-token"),
        { intervalMs: 0 },
      ),
    ).rejects.toBeInstanceOf(TypeError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries transient failures until the job completes", async () => {
    const complete = job("complete");
    fetchMock
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(mockErrorResponse(503))
      .mockResolvedValueOnce(mockErrorResponse(401))
      .mockResolvedValueOnce(mockResponse(complete));
    const onUpdate = vi.fn();

    await expect(
      pollRepositoryIngestion(42, vi.fn().mockResolvedValue("my-token"), {
        intervalMs: 0,
        retryTransientErrors: true,
        onUpdate,
      }),
    ).resolves.toEqual(complete);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });

  it("backs off between failures and resets after a successful poll", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(mockErrorResponse(500))
      .mockResolvedValueOnce(mockErrorResponse(502))
      .mockResolvedValueOnce(mockErrorResponse(503))
      .mockResolvedValueOnce(mockResponse(job("running")))
      .mockResolvedValueOnce(mockErrorResponse(500))
      .mockResolvedValueOnce(mockResponse(job("complete")));

    const polling = pollRepositoryIngestion(
      42,
      vi.fn().mockResolvedValue("my-token"),
      {
        intervalMs: 100,
        maxRetryDelayMs: 250,
        retryTransientErrors: true,
        // Top of the jitter window, so each delay is the full backoff.
        random: () => 1,
      },
    );
    const resolution = expect(polling).resolves.toMatchObject({
      status: "complete",
    });

    // Failures wait 100, 200, then the 250 cap.
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(100);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(200);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(249);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    // A success resets the backoff: regular interval, then first-failure delay.
    await vi.advanceTimersByTimeAsync(100);
    expect(fetchMock).toHaveBeenCalledTimes(5);
    await vi.advanceTimersByTimeAsync(100);
    expect(fetchMock).toHaveBeenCalledTimes(6);

    await resolution;
  });

  it("spreads retries across the upper half of the backoff window", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(mockErrorResponse(500))
      .mockResolvedValueOnce(mockErrorResponse(500))
      .mockResolvedValueOnce(mockResponse(job("complete")));
    const random = vi
      .fn<() => number>()
      .mockReturnValueOnce(0)
      .mockReturnValueOnce(0.5);

    const polling = pollRepositoryIngestion(
      42,
      vi.fn().mockResolvedValue("my-token"),
      { intervalMs: 100, retryTransientErrors: true, random },
    );
    const resolution = expect(polling).resolves.toMatchObject({
      status: "complete",
    });

    // First failure: base 100, random 0 → 50.
    await vi.advanceTimersByTimeAsync(49);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // Second failure: base 200, random 0.5 → 150.
    await vi.advanceTimersByTimeAsync(149);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);

    await resolution;
    expect(random).toHaveBeenCalledTimes(2);
  });

  it("honors Retry-After on a rate-limited status request", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(mockErrorResponse(429, "3"))
      .mockResolvedValueOnce(mockResponse(job("complete")));

    const polling = pollRepositoryIngestion(
      42,
      vi.fn().mockResolvedValue("my-token"),
      { intervalMs: 100, retryTransientErrors: true },
    );
    const resolution = expect(polling).resolves.toMatchObject({
      status: "complete",
    });

    await vi.advanceTimersByTimeAsync(2999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await resolution;
  });

  it("rejects a final client error even when retrying transient ones", async () => {
    fetchMock.mockResolvedValue(mockErrorResponse(404));

    await expect(
      pollRepositoryIngestion(
        42,
        vi.fn().mockResolvedValue("my-token"),
        { intervalMs: 0, retryTransientErrors: true },
      ),
    ).rejects.toMatchObject({ name: "ApiError", status: 404 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps polling past the default window when the timeout is infinite", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(mockResponse(job("running")));

    const polling = pollRepositoryIngestion(
      42,
      vi.fn().mockResolvedValue("my-token"),
      { intervalMs: 60_000, timeoutMs: Infinity },
    );
    const resolution = expect(polling).resolves.toMatchObject({
      status: "complete",
    });

    await vi.advanceTimersByTimeAsync(11 * 60_000);
    expect(fetchMock).toHaveBeenCalledTimes(12);

    fetchMock.mockResolvedValue(mockResponse(job("complete")));
    await vi.advanceTimersByTimeAsync(60_000);

    await resolution;
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
