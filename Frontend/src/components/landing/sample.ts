// Real Camino output shown on the landing page. Every value below was copied
// from the issue preview and the brief Camino produced for jballo/camino #53,
// checked 6 October 2026. Do not add values the product did not show; remove a
// slot instead. The issue belongs to Camino's own repository, so the only
// person named is its maintainer.

export const SAMPLE_REPO = "jballo/camino";
export const SAMPLE_CHECKED_ON = "6 October 2026";

export const SAMPLE_ISSUE_URL = "https://github.com/jballo/camino/issues/53";

export const SAMPLE_DEFAULT_BRANCH = "main";
export const SAMPLE_BRANCH = "dev";
export const SAMPLE_BRANCH_EVIDENCE = "10 of 10 recently merged PRs targeted `dev`";

/** Station 2: what the preview showed before anything was generated. */
export const SAMPLE_SIGNALS = {
  number: 53,
  title: "Allow users to delete issue briefs",
  state: "open",
  warnings: ["This issue is already assigned (jballo)."],
};

/** Station 4: an excerpt of the brief for #53. */
export const SAMPLE_BRIEF = {
  number: 53,
  title: "Allow users to delete issue briefs",
  summary:
    "Implement a feature that allows users to delete their own issue briefs from the home-page workbench. This will require creating a deletion endpoint in the backend and adding UI elements for confirming and executing the deletion in the frontend.",
  counts: [
    { label: "Reading steps", value: "06" },
    { label: "Plan checklist", value: "4 items" },
    { label: "Warnings", value: "01" },
    { label: "Confidence", value: "high" },
  ],
  steps: [
    {
      kind: "Read",
      title: "Backend/app/api/briefs.py:359-365",
      detail: "The GET endpoint for one brief: path parameter, session, authenticated user, ownership check.",
    },
    {
      kind: "Read",
      title: "POST /api/v1/briefs",
      detail: "Backend/app/api/briefs.py:225-230",
    },
    {
      kind: "Read",
      title: "Frontend/src/app/briefs/[id]/page.tsx:52-61",
      detail: "The reader's stop() handler: the cancel call a delete button would sit beside.",
    },
    {
      kind: "Test",
      title: "Test the DELETE endpoint to ensure it correctly removes a brief when the user is authorized.",
    },
    {
      kind: "Plan",
      title: "Clarify behavior for deleting briefs that are currently being processed.",
    },
  ],
  indexedAt: "31afb7e",
} as const;
