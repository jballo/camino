export type RepositoryIngestionStatus =
  | "pending"
  | "running"
  | "complete"
  | "failed"
  | "cancelled";

export type RepositoryIngestionResult = {
  chunks_inserted: number;
  embeddings_created: number;
};

export type RepositoryIngestionCreated = {
  id: number;
  status: RepositoryIngestionStatus;
};

export type RepositoryIngestionJob = {
  id: number;
  status: RepositoryIngestionStatus;
  repoName: string;
  attempts: number;
  result: RepositoryIngestionResult | null;
  error: string | null;
};
