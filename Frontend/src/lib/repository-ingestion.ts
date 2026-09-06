import { ApiError, backendFetch } from "./api";
import type {
  RepositoryIngestionCreated,
  RepositoryIngestionJob,
} from "../types/repository-ingestion";

const DEFAULT_POLL_INTERVAL_MS = 2000;

type PollRepositoryIngestionOptions = {
  intervalMs?: number;
  signal?: AbortSignal;
  onUpdate?: (job: RepositoryIngestionJob) => void;
};

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
    signal,
    onUpdate,
  } = options;

  while (true) {
    if (signal?.aborted) throw abortError();

    const token = await getToken();
    if (!token) throw new ApiError(401, "Not authenticated");

    const job = await backendFetch<RepositoryIngestionJob>(
      `/api/v1/repositories/ingest/${encodeURIComponent(jobId)}`,
      token,
      { signal },
    );
    onUpdate?.(job);

    if (job.status === "complete" || job.status === "failed") return job;

    await wait(intervalMs, signal);
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
