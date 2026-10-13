import { ApiError, backendFetch } from "./api";
import type {
  RepositoryIngestionCreated,
  RepositoryIngestionJob,
} from "../types/repository-ingestion";

const DEFAULT_POLL_INTERVAL_MS = 2000;
const DEFAULT_POLL_TIMEOUT_MS = 10 * 60 * 1000;
const DEFAULT_MAX_RETRY_DELAY_MS = 30 * 1000;

type PollRepositoryIngestionOptions = {
  intervalMs?: number;
  /** Pass `Infinity` to poll until the job settles or the signal aborts. */
  timeoutMs?: number;
  signal?: AbortSignal;
  onUpdate?: (job: RepositoryIngestionJob) => void;
  /**
   * Keep polling through failed status requests (network errors, 5xx, 429,
   * 401) with exponential backoff instead of rejecting on the first one.
   * Other client errors still reject: retrying would not change them.
   */
  retryTransientErrors?: boolean;
  /** Upper bound on the backoff between failed status requests. */
  maxRetryDelayMs?: number;
  /** Source of the retry jitter; tests inject a fixed value. */
  random?: () => number;
};

export class IngestionTimeoutError extends Error {
  constructor() {
    super("Repository ingestion polling timed out");
    this.name = "IngestionTimeoutError";
  }
}

function abortError(): DOMException {
  return new DOMException(
    "Repository ingestion polling was cancelled",
    "AbortError",
  );
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(abortError());

  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);

    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };

    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * A status request that may succeed if repeated. A client error other than
 * 401 (the token may be mid-refresh) or 429 is final for this job.
 */
function isTransientError(error: unknown) {
  if (isAbortError(error)) return false;
  if (!(error instanceof ApiError)) return true;
  if (error.status === 401 || error.status === 429) return true;
  return error.status < 400 || error.status >= 500;
}

/**
 * Exponential backoff with equal jitter: the retry lands in the upper half of
 * the backoff window, so clients that failed together during an outage do
 * not all come back at the same instant. A server-sent Retry-After is kept
 * as is.
 */
function retryDelayMs(
  error: unknown,
  consecutiveFailures: number,
  intervalMs: number,
  maxRetryDelayMs: number,
  random: () => number,
) {
  if (error instanceof ApiError && error.retryAfterSeconds !== undefined) {
    return error.retryAfterSeconds * 1000;
  }
  const base = Math.min(
    intervalMs * 2 ** (consecutiveFailures - 1),
    maxRetryDelayMs,
  );
  return base / 2 + random() * (base / 2);
}

export function enqueueRepositoryIngestion(
  repoName: string,
  ref: string | undefined,
  token: string,
  signal?: AbortSignal,
): Promise<RepositoryIngestionCreated> {
  return backendFetch<RepositoryIngestionCreated>(
    "/api/v1/repositories/ingest",
    token,
    {
      method: "POST",
      body: { repoName, ...(ref ? { ref } : {}) },
      signal,
    },
  );
}

export function cancelRepositoryIngestion(
  jobId: number,
  token: string,
  signal?: AbortSignal,
): Promise<RepositoryIngestionJob> {
  return backendFetch<RepositoryIngestionJob>(
    `/api/v1/repositories/ingest/${encodeURIComponent(jobId)}/cancel`,
    token,
    {
      method: "POST",
      signal,
    },
  );
}

export async function pollRepositoryIngestion(
  jobId: number,
  getToken: () => Promise<string | null>,
  options: PollRepositoryIngestionOptions = {},
): Promise<RepositoryIngestionJob> {
  const {
    intervalMs = DEFAULT_POLL_INTERVAL_MS,
    timeoutMs = DEFAULT_POLL_TIMEOUT_MS,
    signal,
    onUpdate,
    retryTransientErrors = false,
    maxRetryDelayMs = DEFAULT_MAX_RETRY_DELAY_MS,
    random = Math.random,
  } = options;
  const deadline = Date.now() + timeoutMs;
  let consecutiveFailures = 0;

  while (true) {
    if (signal?.aborted) throw abortError();
    if (Date.now() >= deadline) throw new IngestionTimeoutError();

    let job: RepositoryIngestionJob;
    try {
      const token = await getToken();
      if (!token) throw new ApiError(401, "Not authenticated");
      job = await backendFetch<RepositoryIngestionJob>(
        `/api/v1/repositories/ingest/${encodeURIComponent(jobId)}`,
        token,
        { signal },
      );
    } catch (error) {
      if (!retryTransientErrors || !isTransientError(error)) throw error;
      consecutiveFailures += 1;
      const delayMs = retryDelayMs(
        error,
        consecutiveFailures,
        intervalMs,
        maxRetryDelayMs,
        random,
      );
      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) throw new IngestionTimeoutError();
      await wait(Math.min(delayMs, remainingMs), signal);
      continue;
    }
    consecutiveFailures = 0;
    onUpdate?.(job);

    if (
      job.status === "complete" ||
      job.status === "failed" ||
      job.status === "cancelled"
    ) {
      return job;
    }

    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) throw new IngestionTimeoutError();
    await wait(Math.min(intervalMs, remainingMs), signal);
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
