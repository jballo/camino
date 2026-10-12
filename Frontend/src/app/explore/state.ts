import { ApiError, retryWait } from "../../lib/api";

type AnswerSelection = {
  repoName: string;
  ref: string;
};

export function repositorySelectionChanged(
  previousRepo: string | undefined,
  nextRepo: string | undefined,
) {
  return previousRepo !== undefined && previousRepo !== nextRepo;
}

export function answerMatchesSelection(
  answer: AnswerSelection | undefined,
  repoName: string | undefined,
  ref: string | undefined,
) {
  return (
    answer !== undefined && answer.repoName === repoName && answer.ref === ref
  );
}

export function followErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 404) {
    return "Repository not found — or it is private and unavailable to the GitHub App.";
  }
  if (error instanceof ApiError && error.status === 502) {
    return "GitHub check failed — try again.";
  }
  if (error instanceof ApiError && error.status === 429) {
    return `You've queued as many new repositories for indexing as you can for now. Try again ${retryWait(error.retryAfterSeconds)}.`;
  }
  return "Could not add that repository.";
}

export type RepoIngestStatus =
  | "pending"
  | "running"
  | "complete"
  | "failed"
  | "cancelled";

/** The user's latest index request for one repository. */
export type RepoIngest = {
  id: number;
  status: RepoIngestStatus;
  ref: string | null;
  error: string | null;
};

type IngestUpdate = {
  id: number;
  repoName: string;
  status: RepoIngestStatus;
  error: string | null;
};

export function isIngestActive(ingest: RepoIngest | null | undefined) {
  return ingest?.status === "pending" || ingest?.status === "running";
}

export function ingestsFromOverview(
  entries: Array<{ repoName: string; ingest?: RepoIngest | null }>,
): Record<string, RepoIngest> {
  const ingests: Record<string, RepoIngest> = {};
  for (const entry of entries) {
    if (entry.ingest) ingests[entry.repoName] = entry.ingest;
  }
  return ingests;
}

/**
 * Identity of the set of requests still in flight. Polling restarts only when
 * a job joins or leaves the set, not on every status change.
 */
export function activeIngestKey(ingests: Record<string, RepoIngest>) {
  return Object.entries(ingests)
    .filter(([, ingest]) => isIngestActive(ingest))
    .map(([repoName, ingest]) => `${repoName}#${ingest.id}`)
    .sort()
    .join(",");
}

/** The in-flight jobs an {@link activeIngestKey} names. */
export function jobsFromActiveIngestKey(key: string) {
  if (!key) return [];
  return key.split(",").map((item) => {
    const separator = item.lastIndexOf("#");
    return {
      repoName: item.slice(0, separator),
      id: Number(item.slice(separator + 1)),
    };
  });
}

/** Apply a polled job, unless a newer request replaced it meanwhile. */
export function applyIngestUpdate(
  ingests: Record<string, RepoIngest>,
  job: IngestUpdate,
): Record<string, RepoIngest> {
  const current = ingests[job.repoName];
  if (!current || current.id !== job.id) return ingests;
  if (current.status === job.status && current.error === job.error) {
    return ingests;
  }
  return {
    ...ingests,
    [job.repoName]: { ...current, status: job.status, error: job.error },
  };
}

/** Short state for the repository list, shown only while there is no index. */
export function ingestMetaLabel(ingest: RepoIngest | null | undefined) {
  switch (ingest?.status) {
    case "pending":
      return "index queued";
    case "running":
      return "indexing…";
    case "complete":
      return "index ready";
    case "failed":
      return "index failed";
    case "cancelled":
      return "request cancelled";
    default:
      return undefined;
  }
}

/** What the not-indexed panel says about the latest request, if any. */
export function ingestPanelStatus(ingest: RepoIngest | null | undefined):
  | { tone: "progress" | "problem"; message: string; detail?: string }
  | undefined {
  switch (ingest?.status) {
    case "pending":
      return {
        tone: "progress",
        message: "Queued. Indexing starts when a worker picks it up.",
      };
    case "running":
      return {
        tone: "progress",
        message: "Indexing now. This page updates when it finishes.",
      };
    case "complete":
      return { tone: "progress", message: "Index ready. Loading it…" };
    case "failed":
      return {
        tone: "problem",
        message: "Indexing failed.",
        detail: ingest.error ?? undefined,
      };
    case "cancelled":
      return {
        tone: "problem",
        message: "Your index request was cancelled.",
        detail: ingest.error ?? undefined,
      };
    default:
      return undefined;
  }
}

export function requestIndexLabel(ingest: RepoIngest | null | undefined) {
  if (ingest?.status === "pending") return "Queued";
  if (ingest?.status === "running") return "Indexing";
  if (ingest?.status === "failed" || ingest?.status === "cancelled") {
    return "Request again";
  }
  return "Request index";
}
