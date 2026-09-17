import { ApiError, backendFetch } from "./api";
import { isAbortError } from "./repository-ingestion";
import type { BriefPreview, BriefResponse, BriefSummary } from "../types/brief";

export type TokenGetter = () => Promise<string | null>;

const DEFAULT_POLL_INTERVAL_MS = 2000;

type PollIssueBriefOptions = {
  intervalMs?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
  onUpdate?: (brief: BriefResponse) => void;
};

export class BriefPollingTimeoutError extends Error {
  constructor() {
    super("Issue brief polling timed out");
    this.name = "BriefPollingTimeoutError";
  }
}

function abortError(): DOMException {
  return new DOMException("Issue brief polling was cancelled", "AbortError");
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

export { isAbortError };

async function tokenOrThrow(getToken: TokenGetter): Promise<string> {
  const token = await getToken();
  if (!token) throw new ApiError(401, "Not authenticated");
  return token;
}

export async function previewIssueBrief(
  issueUrl: string,
  getToken: TokenGetter,
  targetBranch?: string,
): Promise<BriefPreview> {
  return backendFetch<BriefPreview>(
    "/api/v1/briefs/preview",
    await tokenOrThrow(getToken),
    {
      method: "POST",
      body: { issueUrl, ...(targetBranch ? { targetBranch } : {}) },
    },
  );
}

export async function createIssueBrief(
  issueUrl: string,
  targetBranch: string,
  getToken: TokenGetter,
): Promise<{ id: number; status: string }> {
  return backendFetch("/api/v1/briefs", await tokenOrThrow(getToken), {
    method: "POST",
    body: { issueUrl, targetBranch },
  });
}

export async function getIssueBrief(
  id: number | string,
  getToken: TokenGetter,
  signal?: AbortSignal,
): Promise<BriefResponse> {
  return backendFetch(
    `/api/v1/briefs/${encodeURIComponent(id)}`,
    await tokenOrThrow(getToken),
    { signal },
  );
}

export async function cancelIssueBrief(
  id: number | string,
  getToken: TokenGetter,
): Promise<BriefResponse> {
  return backendFetch(
    `/api/v1/briefs/${encodeURIComponent(id)}/cancel`,
    await tokenOrThrow(getToken),
    { method: "POST" },
  );
}

export async function listIssueBriefs(
  getToken: TokenGetter,
  signal?: AbortSignal,
): Promise<BriefSummary[]> {
  return backendFetch("/api/v1/briefs", await tokenOrThrow(getToken), {
    signal,
  });
}

export async function pollIssueBrief(
  id: number | string,
  getToken: TokenGetter,
  options: PollIssueBriefOptions = {},
): Promise<BriefResponse> {
  const {
    intervalMs = DEFAULT_POLL_INTERVAL_MS,
    timeoutMs,
    signal,
    onUpdate,
  } = options;
  const deadline =
    timeoutMs === undefined ? undefined : Date.now() + timeoutMs;

  while (true) {
    if (signal?.aborted) throw abortError();
    if (deadline !== undefined && Date.now() >= deadline) {
      throw new BriefPollingTimeoutError();
    }

    const brief = await getIssueBrief(id, getToken, signal);
    onUpdate?.(brief);

    if (
      brief.status === "complete" ||
      brief.status === "failed" ||
      brief.status === "cancelled"
    ) {
      return brief;
    }

    if (deadline === undefined) {
      await wait(intervalMs, signal);
      continue;
    }

    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) throw new BriefPollingTimeoutError();
    await wait(Math.min(intervalMs, remainingMs), signal);
  }
}
