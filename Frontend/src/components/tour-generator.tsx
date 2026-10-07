"use client";

import {
  Button,
  Description,
  Dialog,
  DialogPanel,
  DialogTitle,
  Label,
  Radio,
  RadioGroup,
  Textarea,
} from "@headlessui/react";
import { useAuth } from "@clerk/nextjs";
import {
  CheckCircleIcon,
  FileCode,
  Loader2,
  Sparkles,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, useTransition } from "react";

import { ApiError, backendFetch } from "@/lib/api";
import { fetchContributionTarget } from "@/lib/contribution-target";
import {
  cancelRepositoryIngestion,
  enqueueRepositoryIngestion,
  IngestionTimeoutError,
  isAbortError,
  pollRepositoryIngestion,
} from "@/lib/repository-ingestion";
import type { ContributionTarget } from "@/types/contribution-target";
import type { RepositoryIngestionJob } from "@/types/repository-ingestion";

const EXAMPLE_TOPICS = [
  "Authentication flow",
  "Request lifecycle",
  "How data is persisted",
];

export default function TourGenerator() {
  const router = useRouter();
  const { getToken } = useAuth();
  const [prompt, setPrompt] = useState("");
  const [repoSelectionDialog, setRepoSelectionDialog] = useState(false);
  const [repoSelected, setRepoSelected] = useState<undefined | string>(
    undefined,
  );
  const [repos, setRepos] = useState<string[]>([]);
  const [publicRepo, setPublicRepo] = useState("");
  const [repoRetrievalError, setRepoRetrievalError] = useState<
    string | undefined
  >(undefined);
  const [isPending, startTransition] = useTransition();
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | undefined>(undefined);
  const [processing, setProcessing] = useState(false);
  const [processingError, setProcessingError] = useState<string | undefined>(
    undefined,
  );
  const [ingestionJob, setIngestionJob] = useState<
    RepositoryIngestionJob | undefined
  >(undefined);
  const ingestionAbortRef = useRef<AbortController | null>(null);
  const [contributionTarget, setContributionTarget] = useState<
    ContributionTarget | undefined
  >(undefined);
  const [contributionTargetLoading, setContributionTargetLoading] =
    useState(false);

  const canSubmit = prompt.trim().length > 0 && !!repoSelected && !submitting;

  const onSubmitPrompt = useCallback(async () => {
    setSubmitError(undefined);
    try {
      if (
        prompt.length <= 0 ||
        repoSelected == undefined ||
        repoSelected.length <= 0
      )
        throw new Error(`Invalid input`);

      setSubmitting(true);

      const token = await getToken();
      if (!token) throw new ApiError(401, "Not authenticated");

      const result = await backendFetch<{ id: number; status: string }>(
        "/api/v1/journeys",
        token,
        {
          method: "POST",
          body: {
            repoName: repoSelected,
            ...(contributionTarget?.targetBranch
              ? { ref: contributionTarget.targetBranch }
              : {}),
            topic: prompt,
          },
        },
      );
      router.push(`/generate?id=${result.id}`);
    } catch (error) {
      console.log("error: ", error);
      if (
        error instanceof ApiError &&
        (error.status === 401 || error.status === 403)
      ) {
        setSubmitError(
          "Your session expired. Please refresh the page and log in again.",
        );
      } else {
        setSubmitError(
          error instanceof ApiError
            ? error.message
            : "Failed to start tour generation. Select a repo and try again.",
        );
      }
    } finally {
      setSubmitting(false);
    }
  }, [contributionTarget, getToken, repoSelected, prompt, router]);

  const openDialog = async () => {
    setRepoSelectionDialog(true);
    setRepoRetrievalError(undefined);
    setProcessingError(undefined);
    setIngestionJob(undefined);
    startTransition(async () => {
      try {
        const token = await getToken();
        if (!token) throw new ApiError(401, "Not authenticated");

        const result = await backendFetch<string[]>(
          "/api/v1/repositories",
          token,
        );
        setRepos(result);
      } catch (error) {
        console.log("Error: ", error);
        setRepoRetrievalError("Failed to retrieve repositories");
      }
    });
  };

  const processRepo = useCallback(async () => {
    if (repoSelected == undefined) return;

    ingestionAbortRef.current?.abort();
    const controller = new AbortController();
    ingestionAbortRef.current = controller;

    setProcessing(true);
    setProcessingError(undefined);
    setIngestionJob(undefined);
    try {
      const token = await getToken();
      if (!token) throw new ApiError(401, "Not authenticated");

      const created = await enqueueRepositoryIngestion(
        repoSelected,
        contributionTarget?.targetBranch ?? undefined,
        token,
        controller.signal,
      );
      setIngestionJob({
        ...created,
        repoName: repoSelected,
        ref: contributionTarget?.targetBranch ?? null,
        attempts: 0,
        result: null,
        error: null,
      });

      const job = await pollRepositoryIngestion(created.id, getToken, {
        signal: controller.signal,
        onUpdate: setIngestionJob,
      });
      if (job.status === "cancelled") return;
      if (job.status === "failed") {
        throw new Error(job.error ?? "Repository ingestion failed.");
      }
      if (!job.result) {
        throw new Error("Repository ingestion completed without a result.");
      }

      setRepoSelectionDialog(false);
    } catch (error) {
      if (isAbortError(error)) return;
      console.log("Error: ", error);
      if (error instanceof IngestionTimeoutError) {
        setProcessingError(
          "Still queued — the ingestion worker may be unavailable. Try again later.",
        );
      } else if (
        error instanceof ApiError &&
        (error.status === 401 || error.status === 403)
      ) {
        setProcessingError(
          "Your session expired. Please refresh the page and log in again.",
        );
      } else {
        setProcessingError(
          error instanceof Error
            ? error.message
            : "Failed to process repository.",
        );
      }
    } finally {
      if (ingestionAbortRef.current === controller) {
        ingestionAbortRef.current = null;
        setProcessing(false);
      }
    }
  }, [contributionTarget, getToken, repoSelected]);

  const stopRepositoryIngestion = useCallback(async () => {
    const job = ingestionJob;
    const controller = ingestionAbortRef.current;
    if (!job || !controller) return;

    setProcessingError(undefined);
    try {
      const token = await getToken();
      if (!token) throw new ApiError(401, "Not authenticated");

      const cancelledJob = await cancelRepositoryIngestion(job.id, token);
      setIngestionJob(cancelledJob);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        return;
      }
      setProcessingError(
        error instanceof Error
          ? `Failed to stop repository ingestion: ${error.message}`
          : "Failed to stop repository ingestion.",
      );
      return;
    }

    controller.abort();
    if (ingestionAbortRef.current === controller) {
      ingestionAbortRef.current = null;
      setProcessing(false);
    }
  }, [getToken, ingestionJob]);

  useEffect(
    () => () => {
      ingestionAbortRef.current?.abort();
    },
    [],
  );

  useEffect(() => {
    setContributionTarget(undefined);
    if (!repoSelected) {
      setContributionTargetLoading(false);
      return;
    }

    const controller = new AbortController();
    setContributionTargetLoading(true);
    void fetchContributionTarget(repoSelected, getToken, controller.signal)
      .then(setContributionTarget)
      .catch((error: unknown) => {
        if (!isAbortError(error)) {
          console.log("Failed to discover contribution target: ", error);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setContributionTargetLoading(false);
      });

    return () => controller.abort();
  }, [getToken, repoSelected]);

  const contributionTargetSource = (() => {
    if (!contributionTarget?.targetBranch) return undefined;
    if (
      contributionTarget.source === "contributing_doc" ||
      contributionTarget.source === "pr_template"
    ) {
      return contributionTarget.evidencePath
        ? `from ${contributionTarget.evidencePath}`
        : "from repository guidance";
    }
    if (contributionTarget.source === "merged_prs") {
      return "based on recent merged PRs";
    }
    if (contributionTarget.source === "default_branch") {
      return "repository default branch";
    }
    return undefined;
  })();

  return (
    <div className="console flex w-full flex-col gap-4 p-5 sm:p-6">
      <div className="flex flex-col gap-2">
        <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          1. Repository
        </label>
        <Button
          onClick={openDialog}
          className="field-control flex w-full items-center gap-2 text-sm hover:border-foreground"
        >
          <FileCode className="size-4 text-muted-foreground" />
          {repoSelected ? (
            <span className="font-mono">{repoSelected}</span>
          ) : (
            <span className="text-muted-foreground">Select a repository…</span>
          )}
        </Button>
        {repoSelected && contributionTargetLoading && (
          <div
            aria-label="Discovering contribution target"
            className="h-4 w-64 animate-pulse rounded bg-accent"
          />
        )}
        {repoSelected &&
          !contributionTargetLoading &&
          contributionTarget?.targetBranch &&
          contributionTargetSource && (
            <div className="rounded-md border border-border bg-accent/30 px-3 py-2 text-xs text-muted-foreground">
              PRs to this project target{" "}
              <code className="font-mono text-foreground">
                {contributionTarget.targetBranch}
              </code>{" "}
              — {contributionTargetSource}
            </div>
          )}
      </div>

      <div className="flex flex-col gap-2">
        <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          2. Tour topic
        </label>
        <Textarea
          placeholder="What should the tour cover? e.g. “How authentication works”"
          className="field-control field-sizing-content min-h-24 max-h-40 w-full resize-none p-4 text-start"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">Try:</span>
          {EXAMPLE_TOPICS.map((topic) => (
            <button
              key={topic}
              type="button"
              onClick={() => setPrompt(topic)}
              className="rounded-full bg-accent px-2.5 py-0.5 text-xs text-muted-foreground hover:text-foreground"
            >
              {topic}
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-muted-foreground">
          {!repoSelected
            ? "Select a repository to continue"
            : prompt.trim().length === 0
              ? "Describe a topic to continue"
              : "Ready to generate"}
        </span>
        <Button
          className="button-primary"
          aria-label="Generate tour"
          onClick={onSubmitPrompt}
          disabled={!canSubmit}
        >
          {submitting ? (
            <Loader2 aria-hidden="true" className="size-4 animate-spin" />
          ) : (
            <Sparkles aria-hidden="true" className="size-4" />
          )}
          Generate tour
        </Button>
      </div>
      {submitError && (
        <div className="text-sm text-destructive">{submitError}</div>
      )}

      <Dialog
        open={repoSelectionDialog}
        onClose={() => {
          if (!processing) setRepoSelectionDialog(false);
        }}
        className="relative z-50"
      >
        <div className="fixed inset-0 flex w-screen items-center justify-center p-4">
          <DialogPanel className="w-full max-w-lg space-y-5 rounded-[14px] border border-border bg-card p-6 shadow-2xl sm:p-8">
            <DialogTitle className="font-display text-2xl font-black uppercase">
              Select repository
            </DialogTitle>
            <Description className="text-sm text-muted-foreground">
              This is the repository the tour will be based on. It must be
              processed (ingested) before a tour can be generated.
            </Description>
            {processing && (
              <div className="flex items-center gap-2 rounded-md border border-border bg-accent/50 p-3 text-sm">
                <Loader2 className="size-4 shrink-0 animate-spin text-primary" />
                <span>
                  {ingestionJob?.status === "pending"
                    ? ingestionJob.attempts > 0
                      ? `Retry queued (attempt ${ingestionJob.attempts})`
                      : "Repository queued"
                    : ingestionJob?.status === "running"
                      ? ingestionJob.attempts > 1
                        ? `Processing repository (attempt ${ingestionJob.attempts})`
                        : "Processing repository"
                      : "Starting repository ingestion"}
                </span>
              </div>
            )}
            {processingError && (
              <div className="text-sm text-destructive">{processingError}</div>
            )}
            <RadioGroup
              value={repoSelected || ""}
              onChange={setRepoSelected}
              className="flex flex-col gap-3"
            >
              {repoRetrievalError && <div>{repoRetrievalError}</div>}
              {isPending && <div>Loading...</div>}
              {repos.map((repo) => (
                <Radio
                  key={repo}
                  value={repo}
                  className="group relative flex h-10 flex-row items-center justify-between rounded-sm p-3 data-checked:bg-secondary"
                >
                  <Label>{repo}</Label>
                  <CheckCircleIcon className="size-5 fill-white opacity-0 transition group-data-checked:opacity-100" />
                </Radio>
              ))}
            </RadioGroup>
            <div className="flex flex-col gap-2 border-t border-border pt-3">
              <label
                htmlFor="public-repository"
                className="text-xs font-medium text-muted-foreground"
              >
                Or enter any public repository
              </label>
              <div className="flex gap-2">
                <input
                  id="public-repository"
                  aria-label="Public repository"
                  placeholder="owner/repo"
                  value={publicRepo}
                  onChange={(event) => setPublicRepo(event.target.value)}
                  className="min-w-0 flex-1 rounded-md border border-border bg-transparent px-3 py-2 font-mono text-sm focus:outline-none focus:ring-1 focus:ring-ring"
                  disabled={processing}
                />
                <Button
                  className="rounded-md border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50"
                  onClick={() => setRepoSelected(publicRepo.trim())}
                  disabled={
                    processing || !/^[^/\s]+\/[^/\s]+$/.test(publicRepo.trim())
                  }
                >
                  Select
                </Button>
              </div>
            </div>
            <div className="flex justify-between gap-3 pt-2">
              <Button
                className="rounded-sm px-3 py-1.5 text-sm hover:bg-accent"
                onClick={() => {
                  if (processing) {
                    void stopRepositoryIngestion();
                  } else {
                    setRepoSelectionDialog(false);
                  }
                }}
              >
                {processing ? "Stop" : "Cancel"}
              </Button>
              <div className="flex gap-2">
                <Button
                  className="flex items-center gap-1.5 rounded-sm border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
                  onClick={() => processRepo()}
                  disabled={!repoSelected || processing}
                >
                  {processing && <Loader2 className="size-3.5 animate-spin" />}
                  {processing
                    ? ingestionJob?.status === "pending"
                      ? "Queued…"
                      : "Processing…"
                    : "Process repo"}
                </Button>
                <Button
                  className="rounded-sm bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
                  onClick={() => setRepoSelectionDialog(false)}
                  disabled={!repoSelected || processing}
                >
                  Use repository
                </Button>
              </div>
            </div>
          </DialogPanel>
        </div>
      </Dialog>
    </div>
  );
}
