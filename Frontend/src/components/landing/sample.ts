// Real Camino output shown on the landing page. Every value below was copied
// from the issue previews and the brief Camino produced for these issues,
// checked 5 October 2026. Do not add values the product did not show; remove a
// slot instead. The other contributor's username is deliberately left out.

export const SAMPLE_REPO = "orthogonalhq/nous-core";
export const SAMPLE_CHECKED_ON = "5 October 2026";

export const SAMPLE_ISSUE_URL = "https://github.com/orthogonalhq/nous-core/issues/316";

export const SAMPLE_BRANCH = "feat/contributor-friendly-inference-provider-surface";
export const SAMPLE_BRANCH_EVIDENCE = "9 of 10 recently merged PRs targeted this branch.";

export type SampleSignal = {
  number: number;
  title: string;
  labels: string[];
  warnings: string[];
  verdict: "taken" | "open";
};

/** Station 2: the issue that was taken, then the one that was not. */
export const SAMPLE_SIGNALS: SampleSignal[] = [
  {
    number: 322,
    title: "Adapter: Cloudflare Workers AI Model Provider",
    labels: ["good first issue", "adapter"],
    warnings: [
      "This issue is already assigned (another contributor).",
      "Someone already has an open PR for this issue (#418).",
    ],
    verdict: "taken",
  },
  {
    number: 316,
    title: "Adapter: LM Studio Model Provider",
    labels: ["good first issue", "adapter"],
    warnings: [],
    verdict: "open",
  },
];

/** Station 4: an excerpt of the brief for #316. */
export const SAMPLE_BRIEF = {
  number: 316,
  title: "Adapter: LM Studio Model Provider",
  summary:
    "The task is to implement the LM Studio Model Provider as a certified provider leaf, adhering to the OpenAI-compatible API and following the updated provider adapter specifications.",
  counts: [
    { label: "Reading steps", value: "05" },
    { label: "Plan checklist", value: "3 items" },
    { label: "Warnings", value: "00" },
  ],
  steps: [
    {
      kind: "Read",
      title: "Provider Definition Resolution",
      path: "self/subcortex/providers/src/provider-definitions.ts:63-71",
    },
    {
      kind: "Read",
      title: "Ollama Provider Creation",
      path: "self/subcortex/providers/src/providers/ollama/provider.ts:6-8",
    },
    {
      kind: "Read",
      title: "ChatCompletionsProvider Class Definition",
      path: "self/subcortex/providers/src/protocols/openai-api/provider.ts:55-88",
    },
    {
      kind: "Setup",
      title: "Create a new directory for the LM Studio Model Provider in",
      path: "self/subcortex/providers/src/providers/",
    },
    {
      kind: "Ask",
      title: "Clarify any additional requirements for the provider leaf with the maintainer.",
    },
  ],
  indexedAt: "839df36",
} as const;
