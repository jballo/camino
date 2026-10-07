export type ContributionTargetSource =
  | "contributing_doc"
  | "pr_template"
  | "merged_prs"
  | "default_branch"
  | "unresolved";

export type ContributionTarget = {
  repoName: string;
  targetBranch: string | null;
  source: ContributionTargetSource;
  evidence: string | null;
  evidencePath: string | null;
  defaultBranch: string | null;
  checkedAt: string;
};
