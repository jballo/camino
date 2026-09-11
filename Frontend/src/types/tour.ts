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

export type TourArtifact = {
  title: string;
  topic: string;
  repo_name: string;
  steps: TourStep[];
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
  topic: string;
  artifact: TourArtifact | null;
  error: string | null;
};

export type JourneySummary = {
  id: number;
  status: JourneyStatus;
  repoName: string;
  topic: string;
  createdAt: string;
};
