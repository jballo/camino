import type { JourneyStatus, TourFreshness, TourStep } from "./tour";

export type BriefWarning = { kind: string; message: string; url: string | null };

export type BriefPreview = {
  issueUrl: string;
  repoName: string;
  issueNumber: number;
  title: string;
  state: string;
  labels: string[];
  assignees: string[];
  warnings: BriefWarning[];
  targetBranch: {
    branch: string | null;
    source: string;
    evidence: string | null;
    evidencePath: string | null;
    defaultBranch: string | null;
  };
  forkStatus: {
    forkRepo: string | null;
    upstreamRepo: string;
    commitsBehind: number | null;
    measurable: boolean;
  };
};

export type BriefArtifact = {
  issue_title: string;
  issue_number: number;
  repo_name: string;
  summary: string;
  house_rules: string[];
  setup_recipe: {
    steps: string[];
    target_branch: string | null;
    target_branch_source: string;
    target_branch_evidence: string | null;
    target_branch_evidence_path: string | null;
    default_branch: string | null;
    fork_repo: string | null;
    upstream_repo: string;
    fork_commits_behind: number | null;
    fork_status_measurable: boolean;
  };
  reading_steps: TourStep[];
  test_guidance: string[];
  plan_checklist: string[];
  freshness: TourFreshness | null;
  preflight_warnings: BriefWarning[];
  confidence: {
    level: string;
    issue_is_vague: boolean;
    questions_for_maintainer: string[];
  };
  honesty_note: string | null;
};

export type BriefResponse = {
  id: number;
  status: JourneyStatus;
  phase: "blocked_on_ingest" | "queued" | "generating" | JourneyStatus;
  repoName: string;
  ref: string | null;
  issueNumber: number;
  issueTitle: string;
  artifact: BriefArtifact | null;
  error: string | null;
};

export type BriefSummary = Pick<
  BriefResponse,
  "id" | "status" | "repoName" | "ref" | "issueNumber" | "issueTitle"
> & { createdAt: string };
