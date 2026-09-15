export type TourStep = {
  title: string;
  explanation: string;
  file_path: string;
  start_line: number;
  end_line: number;
  snippet: string;
  why: string | null;
  language?: string | null;
};

export type TourFreshness = {
  indexed_sha: string | null;
  head_sha: string | null;
  commits_behind: number | null;
  measurable: boolean;
  changed_cited_files: string[];
  checked_at: string;
};

export type TourArtifact = {
  title: string;
  topic: string;
  repo_name: string;
  steps: TourStep[];
  freshness?: TourFreshness | null;
};

export type JourneyStatus =
  | "pending"
  | "generating"
  | "running"
  | "complete"
  | "failed"
  | "cancelled";

export type JourneyResponse = {
  id: number;
  status: JourneyStatus;
  repoName: string;
  ref: string | null;
  topic: string;
  artifact: TourArtifact | null;
  error: string | null;
};

export type JourneySummary = {
  id: number;
  status: JourneyStatus;
  repoName: string;
  ref: string | null;
  topic: string;
  createdAt: string;
};
