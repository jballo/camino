"use client";

import { Button, Label, Radio, RadioGroup, Textarea } from "@headlessui/react";
import {
  CheckCircleIcon,
  FileCode,
  Loader2,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  Description,
  Dialog,
  DialogPanel,
  DialogTitle,
} from "@headlessui/react";
import { useAuth } from "@clerk/nextjs";

import { ApiError, backendFetch } from "@/lib/api";
import {
  enqueueRepositoryIngestion,
  IngestionTimeoutError,
  isAbortError,
  pollRepositoryIngestion,
} from "@/lib/repository-ingestion";
import type { RepositoryIngestionJob } from "@/types/repository-ingestion";

const EXAMPLE_TOPICS = [
  "Authentication flow",
  "Request lifecycle",
  "How data is persisted",
];

export default function Home() {
  const router = useRouter();
  const { getToken } = useAuth();
  const [prompt, setPrompt] = useState("");
  const [repoSelectionDialog, setRepoSelectionDialog] = useState(false);
  const [repoSelected, setRepoSelected] = useState<undefined | string>(
    undefined,
  );
  const [repos, setRepos] = useState<string[]>([]);
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
          topic: prompt,
        }
      });
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
  }, [getToken, repoSelected, prompt, router]);

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
        token,
        controller.signal,
      );
      setIngestionJob({
        ...created,
        repoName: repoSelected,
        attempts: 0,
        result: null,
        error: null,
      });

      const job = await pollRepositoryIngestion(created.id, getToken, {
        signal: controller.signal,
        onUpdate: setIngestionJob,
      });
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
  }, [getToken, repoSelected]);

  useEffect(
    () => () => {
      ingestionAbortRef.current?.abort();
    },
    [],
  );

  return (
    <div className="flex flex-col justify-center items-center w-full min-h-full">
      <div className="flex flex-col justify-center items-center w-full max-w-[760px] px-8 py-12 gap-3">
          <div className="flex flex-col items-center gap-2 text-center">
            <h2 className="text-4xl">Generate a guided tour</h2>
            <p className="text-sm text-muted-foreground max-w-md">
              Pick a repository, describe a topic, and Camino builds an ordered,
              code-grounded walkthrough of how it works.
            </p>
          </div>
          <div className="flex flex-col outline-1 outline-accent rounded-2xl p-5 gap-4 w-full">
            {/* Step 1: repository */}
            <div className="flex flex-col gap-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                1. Repository
              </label>
              <Button
                onClick={openDialog}
                className="flex items-center gap-2 self-start rounded-md border border-border px-3 py-2 text-sm hover:bg-accent"
              >
                <FileCode className="size-4 text-muted-foreground" />
                {repoSelected ? (
                  <span className="font-mono">{repoSelected}</span>
                ) : (
                  <span className="text-muted-foreground">Select a repository…</span>
                )}
              </Button>
            </div>

            {/* Step 2: topic */}
            <div className="flex flex-col gap-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                2. Tour topic
              </label>
              <Textarea
                placeholder="What should the tour cover? e.g. “How authentication works”"
                className="w-full rounded-md border border-border p-3 text-start focus:outline-none focus:ring-1 focus:ring-ring field-sizing-content min-h-16 max-h-40 resize-none"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
              />
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">Try:</span>
                {EXAMPLE_TOPICS.map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setPrompt(t)}
                    className="rounded-full bg-accent px-2.5 py-0.5 text-xs text-muted-foreground hover:text-foreground"
                  >
                    {t}
                  </button>
                ))}
              </div>
            </div>

            {/* Step 3: generate */}
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs text-muted-foreground">
                {!repoSelected
                  ? "Select a repository to continue"
                  : prompt.trim().length === 0
                    ? "Describe a topic to continue"
                    : "Ready to generate"}
              </span>
              <Button
                className="flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50"
                aria-label="Generate tour"
                onClick={onSubmitPrompt}
                disabled={!canSubmit}
              >
                {submitting ? (
                  <Loader2 aria-hidden="true" className="animate-spin size-4" />
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
                  <DialogPanel className="max-w-lg space-y-4 border bg-primary-foreground p-12 rounded-md">
                    <DialogTitle className="font-bold">
                      Select repository
                    </DialogTitle>
                    <Description className="text-sm text-muted-foreground">
                      This is the repository the tour will be based on. It must
                      be processed (ingested) before a tour can be generated.
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
                      <div className="text-sm text-destructive">
                        {processingError}
                      </div>
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
                          className="group flex flex-row items-center justify-between relative data-checked:bg-secondary h-10 p-3 rounded-sm"
                        >
                          <Label>{repo}</Label>
                          <CheckCircleIcon className="size-5 fill-white opacity-0 transition group-data-checked:opacity-100" />
                        </Radio>
                      ))}
                    </RadioGroup>
                    <div className="flex justify-between gap-3 pt-2">
                      <Button
                        className="rounded-sm px-3 py-1.5 text-sm hover:bg-accent"
                        onClick={() => {
                          if (processing) {
                            ingestionAbortRef.current?.abort();
                          } else {
                            setRepoSelectionDialog(false);
                          }
                        }}
                      >
                        {processing ? "Stop waiting" : "Cancel"}
                      </Button>
                      <div className="flex gap-2">
                        <Button
                          className="flex items-center gap-1.5 rounded-sm border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
                          onClick={() => processRepo()}
                          disabled={!repoSelected || processing}
                        >
                          {processing && (
                            <Loader2 className="size-3.5 animate-spin" />
                          )}
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
        </div>
    </div>
  );
}
