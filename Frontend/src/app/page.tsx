"use client";

import { Button, Input } from "@headlessui/react";
import { useAuth } from "@clerk/nextjs";
import {
  AlertTriangle,
  ExternalLink,
  Loader2,
  RotateCw,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import BriefPane from "@/components/brief-pane";
import BriefRail from "@/components/brief-rail";
import { ChakanaMark, LlamaTrail, Scribble, TileCluster } from "@/components/trail-art";
import { ApiError } from "@/lib/api";
import {
  cancelIssueBrief,
  createIssueBrief,
  getIssueBrief,
  isAbortError,
  listIssueBriefs,
  pollIssueBrief,
  previewIssueBrief,
} from "@/lib/briefs";
import type { BriefPreview, BriefResponse, BriefSummary } from "@/types/brief";

const TERMINAL_STATUSES = new Set(["complete", "failed", "cancelled"]);

const STEPS = [
  {
    number: "02",
    title: "Contribution signals",
    body: "Checked before anything is generated.",
  },
  {
    number: "03",
    title: "The right branch",
    body: "Camino finds the branch to work from.",
  },
  {
    number: "04",
    title: "Implementation brief",
    body: "A grounded brief, generated from the repository.",
  },
] as const;

function createErrorMessage(caught: unknown, fallback: string) {
  if (caught instanceof ApiError && caught.status === 429) {
    return "Too many brief requests right now. Please wait a minute and try again.";
  }
  return caught instanceof Error ? caught.message : fallback;
}

function briefListErrorMessage(caught: unknown) {
  if (
    caught instanceof ApiError &&
    (caught.status === 401 || caught.status === 403)
  ) {
    return "We couldn't load your briefs because your session is unavailable. Sign in again, then retry.";
  }
  return caught instanceof ApiError
    ? `We couldn't load your briefs: ${caught.message}`
    : "We couldn't load your briefs. Check your connection and try again.";
}

export default function Home() {
  const { getToken } = useAuth();
  const [issueUrl, setIssueUrl] = useState("");
  const [preview, setPreview] = useState<BriefPreview>();
  const [branch, setBranch] = useState("");
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string>();
  const [briefs, setBriefs] = useState<BriefSummary[]>([]);
  const [briefsLoading, setBriefsLoading] = useState(true);
  const [briefsError, setBriefsError] = useState<string>();
  const [selectedId, setSelectedIdState] = useState<number>();
  const [selectionVersion, setSelectionVersion] = useState(0);
  const [selectedBrief, setSelectedBrief] = useState<BriefResponse>();
  const [paneLoading, setPaneLoading] = useState(false);
  const [paneError, setPaneError] = useState<string>();
  const paneAbortRef = useRef<AbortController | undefined>(undefined);
  const selectedIdRef = useRef<number | undefined>(undefined);

  const setSelectedId = useCallback((id: number) => {
    selectedIdRef.current = id;
    setSelectedIdState(id);
  }, []);

  const loadBriefs = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const result = await listIssueBriefs(getToken, signal);
        setBriefs(result);
        setBriefsError(undefined);
        setSelectedIdState((current) => {
          const next = current ?? result[0]?.id;
          selectedIdRef.current = next;
          return next;
        });
      } catch (caught) {
        if (isAbortError(caught)) return;
        setBriefsError(briefListErrorMessage(caught));
      } finally {
        if (!signal?.aborted) setBriefsLoading(false);
      }
    },
    [getToken],
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadBriefs(controller.signal);
    return () => controller.abort();
  }, [loadBriefs]);

  useEffect(() => {
    paneAbortRef.current?.abort();
    if (selectedId === undefined) {
      setSelectedBrief(undefined);
      setPaneError(undefined);
      setPaneLoading(false);
      return;
    }

    const controller = new AbortController();
    paneAbortRef.current = controller;
    setSelectedBrief(undefined);
    setPaneError(undefined);
    setPaneLoading(true);

    async function loadSelectedBrief() {
      try {
        const initial = await getIssueBrief(selectedId!, getToken, controller.signal);
        if (controller.signal.aborted) return;
        setSelectedBrief(initial);
        setPaneLoading(false);

        if (TERMINAL_STATUSES.has(initial.status)) {
          await loadBriefs(controller.signal);
          return;
        }

        const terminal = await pollIssueBrief(selectedId!, getToken, {
          signal: controller.signal,
          onUpdate: (update) => {
            if (!controller.signal.aborted) setSelectedBrief(update);
          },
        });
        if (controller.signal.aborted) return;
        setSelectedBrief(terminal);
        await loadBriefs(controller.signal);
      } catch (caught) {
        if (isAbortError(caught)) return;
        setPaneError(
          caught instanceof ApiError
            ? caught.message
            : "Failed to load this issue brief.",
        );
      } finally {
        if (!controller.signal.aborted) setPaneLoading(false);
      }
    }

    void loadSelectedBrief();
    return () => controller.abort();
  }, [getToken, loadBriefs, selectedId, selectionVersion]);

  const selectedSummary = useMemo(
    () => briefs.find((brief) => brief.id === selectedId),
    [briefs, selectedId],
  );
  const activeBriefCount = briefs.filter((brief) =>
    ["pending", "running", "generating"].includes(brief.status),
  ).length;

  async function inspectIssue() {
    if (!issueUrl.trim()) return;
    setLoading(true);
    setError(undefined);
    setPreview(undefined);
    try {
      const result = await previewIssueBrief(issueUrl.trim(), getToken);
      setPreview(result);
      setBranch(result.targetBranch.branch ?? "");
    } catch (caught) {
      setError(
        caught instanceof ApiError &&
          (caught.status === 401 || caught.status === 403)
          ? "Sign in to preview an issue."
          : caught instanceof ApiError
            ? caught.message
            : "We couldn't inspect this issue.",
      );
    } finally {
      setLoading(false);
    }
  }

  function retryBriefs() {
    setBriefsError(undefined);
    setBriefsLoading(true);
    void loadBriefs();
  }

  async function selectCreatedBrief(id: number) {
    setPreview(undefined);
    setIssueUrl("");
    setBranch("");
    await loadBriefs();
    setSelectedId(id);
    setSelectionVersion((current) => current + 1);
  }

  async function generate() {
    if (!preview || !branch.trim()) return;
    setCreating(true);
    setError(undefined);
    try {
      const result = await createIssueBrief(
        preview.issueUrl,
        branch.trim(),
        getToken,
      );
      await selectCreatedBrief(result.id);
    } catch (caught) {
      setError(createErrorMessage(caught, "Failed to start the issue brief."));
    } finally {
      setCreating(false);
    }
  }

  async function cancelBrief(brief: BriefResponse) {
    setPaneError(undefined);
    try {
      const result = await cancelIssueBrief(brief.id, getToken);
      if (selectedIdRef.current === brief.id) setSelectedBrief(result);
      await loadBriefs();
    } catch (caught) {
      if (selectedIdRef.current !== brief.id) return;
      setPaneError(
        caught instanceof ApiError
          ? caught.message
          : "Failed to cancel this issue brief.",
      );
    }
  }

  async function regenerateBrief(brief: BriefResponse) {
    setPaneError(undefined);
    const reconstructedUrl = `https://github.com/${brief.issueRepo}/issues/${brief.issueNumber}`;
    try {
      let targetBranch = brief.ref;
      if (!targetBranch) {
        const branchPreview = await previewIssueBrief(reconstructedUrl, getToken);
        targetBranch = branchPreview.targetBranch.branch;
      }
      if (!targetBranch) {
        throw new Error("Camino couldn't resolve a target branch for this issue.");
      }

      const result = await createIssueBrief(
        reconstructedUrl,
        targetBranch,
        getToken,
      );
      await selectCreatedBrief(result.id);
    } catch (caught) {
      setPaneError(
        createErrorMessage(caught, "Failed to regenerate this issue brief."),
      );
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-[1080px] flex-col px-5 pb-32 pt-10 sm:px-8 lg:px-[66px] lg:pt-[42.25px]">
      <header className="relative">
        <div className="flex items-center gap-[9px]">
          <ChakanaMark className="shrink-0 text-brand-accent" />
          <span className="eyebrow">Open-source contribution helper</span>
        </div>
        <h1 className="mt-[17.75px] font-shade text-[clamp(36px,8.2vw,88.5px)] uppercase leading-[1.04] tracking-[.01em] text-foreground">
          <span className="block">Solve</span>
          <span className="block">your first</span>
          <span className="block text-brand-accent">issue</span>
        </h1>
        <Scribble className="text-foreground" />
        <p className="mt-[17.5px] max-w-[480px] text-[13.5px] leading-[20.25px] text-muted-foreground">
          Paste a GitHub issue. Camino checks contribution signals, finds the
          right branch, and generates a grounded implementation brief.
        </p>
        <TileCluster className="absolute right-0 top-0 hidden size-0 lg:block" />
      </header>

      <LlamaTrail className="mt-[40px]" />

      <div className="mt-[39px] grid grid-cols-1 items-center gap-10 lg:grid-cols-[minmax(0,1fr)_319px] lg:gap-[45.5px]">
        <section
          className="relative rounded-[12px_14px_16px_12px] border-2 border-brand-accent bg-card"
          aria-labelledby="new-brief-heading"
        >
          <span
            aria-hidden="true"
            className="absolute -left-[14px] -top-[15.75px] flex size-[36px] items-center justify-center rounded-full bg-brand-accent font-display text-[11.5px] text-background"
          >
            <span className="-rotate-[8deg]">01</span>
          </span>
          <div className="flex h-[47px] items-center justify-between gap-4 border-b border-border pl-[46px] pr-[24px]">
            <h2 id="new-brief-heading" className="font-display text-[11.5px] uppercase tracking-[.045em]">
              Paste a GitHub issue URL
            </h2>
            <span className="eyebrow hidden tracking-[.18em] sm:inline">Any public repository</span>
          </div>
          <div className="flex flex-col items-stretch gap-[9px] px-6 pt-[15.25px] sm:flex-row sm:items-center">
            <label htmlFor="issue-url" className="sr-only">
              GitHub issue URL
            </label>
            <Input
              id="issue-url"
              type="url"
              value={issueUrl}
              onChange={(event) => setIssueUrl(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void inspectIssue();
              }}
              placeholder="https://github.com/owner/repo/issues/123"
              className="field-control min-h-[42.25px] min-w-0 flex-1 px-[14px] font-mono text-[11.25px] placeholder:text-muted-foreground"
            />
            <Button
              onClick={inspectIssue}
              disabled={loading || !issueUrl.trim()}
              className="button-primary min-h-[42.25px] w-full shrink-0 px-0 tracking-[.09em] sm:w-[143px]"
            >
              {loading && <Loader2 aria-hidden="true" className="size-4 animate-spin" />}
              Preview issue
            </Button>
          </div>
          <p className="px-6 pb-[18.25px] pt-[9.75px] font-mono text-[9px] text-muted-foreground">
            Preflight checks run before anything is generated.
          </p>
          {error && (
            <p role="alert" className="border-t border-border px-6 py-3 text-sm text-destructive">
              {error}
            </p>
          )}
        </section>

        <ol className="relative flex flex-col gap-[23px] pb-[5px] pt-[10px]" aria-label="What happens next">
          <span aria-hidden="true" className="absolute bottom-[2px] left-[11px] top-[5px] w-[12.5px] border-x-2 border-rail-dim" />
          {STEPS.map((step, index) => (
            <li key={step.number} className="relative flex items-center gap-[19.75px]">
              {index > 0 && (
                <span aria-hidden="true" className="absolute -top-[13px] left-[11px] h-[1.5px] w-[12.5px] bg-rail-dim" />
              )}
              <span
                aria-hidden="true"
                className="relative flex size-[35px] shrink-0 items-center justify-center rounded-full border-[2.25px] border-rail-dim bg-background font-display text-[12.5px] text-brand-accent"
              >
                {step.number}
              </span>
              <div className="flex flex-col gap-[4px]">
                <h3 className="font-display text-[11.9px] uppercase leading-[15px]">{step.title}</h3>
                <p className="text-[11.25px] leading-[15.5px] text-muted-foreground">{step.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>

      {preview && (
        <section className="console mt-10 flex flex-col gap-5 p-6" aria-label="Issue preview">
          <div className="flex flex-col gap-2">
            <div className="eyebrow">
              {preview.issueRepo} · issue #{preview.issueNumber} · {preview.state}
            </div>
            <h2 className="text-2xl font-medium">{preview.title}</h2>
            <div className="flex flex-wrap gap-2">
              {preview.labels.map((label) => (
                <span key={label} className="rounded-[4px] border border-input px-2 py-1 font-mono text-[11px]">
                  {label}
                </span>
              ))}
            </div>
          </div>

          {preview.warnings.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {preview.warnings.map((warning, index) => {
                const chip = (
                  <span className="inline-flex items-center gap-1.5 rounded-[4px] border border-dashed border-warning/50 px-3 py-1.5 text-xs text-warning">
                    <AlertTriangle aria-hidden="true" className="size-3.5" />
                    {warning.message}
                    {warning.url && <ExternalLink aria-hidden="true" className="size-3" />}
                  </span>
                );
                return warning.url ? (
                  <a
                    key={`${warning.kind}-${index}`}
                    href={warning.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {chip}
                  </a>
                ) : (
                  <span key={`${warning.kind}-${index}`}>{chip}</span>
                );
              })}
            </div>
          )}

          <div className="rounded-[10px] border border-border bg-background/60 p-4">
            <label htmlFor="target-branch" className="font-display text-[11.5px] uppercase">
              PRs to this project target
            </label>
            <div className="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center">
              <Input
                id="target-branch"
                value={branch}
                onChange={(event) => setBranch(event.target.value)}
                className="field-control w-full font-mono text-sm sm:w-64"
              />
              <span className="text-xs text-muted-foreground">
                {branch === preview.targetBranch.branch
                  ? preview.targetBranch.evidence ?? preview.targetBranch.source
                  : "Changed by you"}
              </span>
            </div>
            {branch === preview.targetBranch.branch &&
              preview.forkStatus.measurable &&
              preview.forkStatus.forkRepo && (
                <p className="mt-3 text-xs text-muted-foreground">
                  Your fork is {preview.forkStatus.commitsBehind} commits behind
                  upstream/{branch}.
                </p>
              )}
          </div>

          <Button
            onClick={generate}
            disabled={creating || !branch.trim()}
            className="button-primary w-fit"
          >
            {creating && <Loader2 aria-hidden="true" className="size-4 animate-spin" />}
            Generate brief
          </Button>
        </section>
      )}

      <section className="mt-[34.75px] flex flex-col gap-[9.25px]" aria-labelledby="briefs-heading">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="briefs-heading" className="eyebrow">
            Your briefs — {String(briefs.length).padStart(2, "0")}
          </h2>
          <span className="eyebrow">
            {String(activeBriefCount).padStart(2, "0")} generating
          </span>
        </div>

        {briefsError && briefs.length > 0 && (
          <BriefListError message={briefsError} onRetry={retryBriefs} />
        )}

        {briefsLoading && briefs.length === 0 ? (
          <div className="console console-cell flex min-h-40 items-center justify-center gap-3 text-sm text-muted-foreground">
            <Loader2 aria-hidden="true" className="size-5 animate-spin" />
            Loading your briefs
          </div>
        ) : briefsError && briefs.length === 0 ? (
          <BriefListError message={briefsError} onRetry={retryBriefs} />
        ) : briefs.length === 0 ? (
          <div className="console console-cell min-h-32">
            <p className="text-sm text-muted-foreground">
              No briefs yet — paste an issue URL to generate your first one.
            </p>
          </div>
        ) : (
          <div className="grid min-w-0 grid-cols-1 items-start gap-4 min-[900px]:grid-cols-[340px_minmax(0,1fr)]">
            <BriefRail
              briefs={briefs}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
            <BriefPane
              key={selectedId}
              brief={selectedBrief}
              createdAt={selectedSummary?.createdAt}
              loading={paneLoading}
              error={paneError}
              onCancel={cancelBrief}
              onRegenerate={regenerateBrief}
            />
          </div>
        )}
      </section>
    </div>
  );
}

function BriefListError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex min-h-[86.25px] flex-col items-start justify-center gap-4 border-[1.5px] border-dashed border-[#2e3033] bg-card px-[22px] pb-[18px] pt-[14px] sm:flex-row sm:items-center sm:justify-between">
      <div role="alert" className="flex items-start gap-[13px]">
        <AlertTriangle
          aria-hidden="true"
          strokeWidth={2.25}
          className="mt-[1px] size-[15px] shrink-0 text-destructive"
        />
        <div className="flex flex-col gap-[5px]">
          <p className="text-[12.8px] font-medium leading-[16px]">Your briefs are unavailable</p>
          <p className="max-w-2xl text-[12px] leading-[16px] text-muted-foreground">{message}</p>
        </div>
      </div>
      <Button onClick={onRetry} className="button-ghost h-[36.25px] w-[92px] shrink-0 gap-[6px] px-0 sm:mt-[6px]">
        <RotateCw aria-hidden="true" strokeWidth={2.25} className="size-[12px]" />
        Retry
      </Button>
    </div>
  );
}
