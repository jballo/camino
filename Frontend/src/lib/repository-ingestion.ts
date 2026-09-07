import { ApiError, backendFetch } from "./api";
import type {
  RepositoryIngestionCreated,
  RepositoryIngestionJob,
} from "../types/repository-ingestion";

const DEFAULT_POLL_INTERVAL_MS = 2000;
const DEFAULT_POLL_TIMEOUT_MS = 10 * 60 * 1000;

type PollRepositoryIngestionOptions = {
  intervalMs?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
  onUpdate?: (job: RepositoryIngestionJob) => void;
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

export function enqueueRepositoryIngestion(
  repoName: string,
  token: string,
  signal?: AbortSignal,
): Promise<RepositoryIngestionCreated> {
  return backendFetch<RepositoryIngestionCreated>(
    "/api/v1/repositories/ingest",
    token,
    {
      method: "POST",
      body: { repoName },
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
  } = options;
  const deadline = Date.now() + timeoutMs;

  while (true) {
    if (signal?.aborted) throw abortError();
    if (Date.now() >= deadline) throw new IngestionTimeoutError();

    const token = await getToken();
    if (!token) throw new ApiError(401, "Not authenticated");

    const job = await backendFetch<RepositoryIngestionJob>(
      `/api/v1/repositories/ingest/${encodeURIComponent(jobId)}`,
      token,
      { signal },
    );
    onUpdate?.(job);

    if (job.status === "complete" || job.status === "failed") return job;

    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) throw new IngestionTimeoutError();
    await wait(Math.min(intervalMs, remainingMs), signal);
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
