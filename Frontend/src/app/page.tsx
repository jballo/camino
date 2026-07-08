"use client";

import Header from "@/components/header";
import { Button, Label, Radio, RadioGroup, Textarea } from "@headlessui/react";
import {
  CheckCircleIcon,
  FileCode,
  Loader2,
  Sparkles,
} from "lucide-react";
import { useCallback, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  Description,
  Dialog,
  DialogPanel,
  DialogTitle,
} from "@headlessui/react";

const EXAMPLE_TOPICS = [
  "Authentication flow",
  "Request lifecycle",
  "How data is persisted",
];

export default function Home() {
  const router = useRouter();
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

      const response = await fetch("/api/journeys", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          repoName: repoSelected,
          topic: prompt,
        }),
      });

      if (!response.ok) {
        const errBody = (await response.json().catch(() => null)) as
          | { error?: string }
          | null;
        if (response.status === 401 || response.status === 403) {
          setSubmitError(
            "Your session expired. Please refresh the page and log in again.",
          );
        } else {
          setSubmitError(
            errBody?.error ??
              "Failed to start tour generation. Select a repo and try again.",
          );
        }
        setSubmitting(false);
        return;
      }

      const result = (await response.json()) as { id: number; status: string };
      router.push(`/generate?id=${result.id}`);
    } catch (error) {
      console.log("error: ", error);
      setSubmitError("Failed to start tour generation. Select a repo and try again.");
      setSubmitting(false);
    }
  }, [repoSelected, prompt, router]);

  const openDialog = async () => {
    setRepoSelectionDialog(true);
    setRepoRetrievalError(undefined);
    startTransition(async () => {
      try {
        const response = await fetch("/api/repositories", {
          method: "GET",
        });

        if (!response.ok) throw new Error("Failed to get repos");
        const result = await response.json();

        setRepos(result);
      } catch (error) {
        console.log("Error: ", error);
        setRepoRetrievalError("Failed to retrieve repositories");
      }
    });
  };

  const processRepo = useCallback(async () => {
    if (repoSelected == undefined) return;

    setProcessing(true);
    try {
      const response = await fetch("/api/repositories/ingest", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          repoName: repoSelected,
        }),
      });

      if (!response.ok) throw new Error("Failed to process repo");

      const result = await response.text();
      console.log("Result: ", result);
      setRepoSelectionDialog(false);
    } catch (error) {
      console.log("Error: ", error);
    } finally {
      setProcessing(false);
    }
  }, [repoSelected]);

  return (
    <div className="flex flex-col w-full h-screen">
      <div className="flex w-full h-1/12">
        <Header />
      </div>
      <div className="flex flex-col justify-center items-center w-full h-11/12">
        <div className="flex flex-col justify-center items-center w-2/3 gap-3 -mt-24 max-w-[760px]">
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
                onClose={() => setRepoSelectionDialog(false)}
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
                        onClick={() => setRepoSelectionDialog(false)}
                      >
                        Cancel
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
                          Process repo
                        </Button>
                        <Button
                          className="rounded-sm bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
                          onClick={() => setRepoSelectionDialog(false)}
                          disabled={!repoSelected}
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
    </div>
  );
}
