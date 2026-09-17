"use client";

import { Button, Input } from "@headlessui/react";
import { useAuth } from "@clerk/nextjs";
import {
  AlertTriangle,
  BookOpen,
  ExternalLink,
  Link as LinkIcon,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import BriefPane from "@/components/brief-pane";
import BriefRail from "@/components/brief-rail";
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
    <div className="page-shell max-w-[1180px] gap-8">
      <header className="flex flex-col items-center gap-3 text-center">
        <div className="flex items-center gap-3">
          <BookOpen aria-hidden="true" className="size-5 text-brand-accent" />
          <span className="eyebrow">Open-source contribution helper</span>
        </div>
        <h1 className="display-title text-5xl font-black sm:text-7xl">
          Solve your first issue<span className="text-brand-accent">.</span>
        </h1>
        <p className="max-w-2xl text-base text-muted-foreground">
          Paste a GitHub issue. Camino checks contribution signals, finds the
          right branch, and generates a grounded implementation brief.
        </p>
      </header>

      <section className="console" aria-label="New brief">
        <div className="console-bar gap-4">
          <span>
            <span className="text-brand-accent">01</span> · Paste a GitHub issue URL
          </span>
          <span className="hidden sm:inline">Any public repository</span>
        </div>
        <div className="flex flex-col items-stretch gap-3 p-5 min-[900px]:flex-row min-[900px]:items-center">
          <label className="field-control flex min-w-0 flex-1 items-center gap-3">
            <span className="sr-only">GitHub issue URL</span>
            <LinkIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
            <Input
              id="issue-url"
              type="url"
              value={issueUrl}
              onChange={(event) => setIssueUrl(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void inspectIssue();
              }}
              placeholder="https://github.com/owner/repo/issues/123"
              className="min-w-0 flex-1 bg-transparent font-mono text-sm outline-none placeholder:text-muted-foreground"
            />
          </label>
          <Button
            onClick={inspectIssue}
            disabled={loading || !issueUrl.trim()}
            className="button-primary shrink-0"
          >
            {loading && <Loader2 aria-hidden="true" className="size-4 animate-spin" />}
            Preview issue
          </Button>
        </div>
        <p className="px-5 pb-4 font-mono text-[11px] text-muted-foreground">
          Preflight checks run before anything is generated.
        </p>
        {error && <p className="border-t border-border px-5 py-3 text-sm text-destructive">{error}</p>}
      </section>

      {preview && (
        <section className="console flex flex-col gap-5 p-6">
          <div className="flex flex-col gap-2">
            <div className="text-xs uppercase tracking-wide text-muted-foreground">
              {preview.issueRepo} · issue #{preview.issueNumber} · {preview.state}
            </div>
            <h2 className="text-2xl font-semibold">{preview.title}</h2>
            <div className="flex flex-wrap gap-2">
              {preview.labels.map((label) => (
                <span key={label} className="rounded-full bg-accent px-2 py-1 text-xs">
                  {label}
                </span>
              ))}
            </div>
          </div>

          {preview.warnings.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {preview.warnings.map((warning, index) => {
                const chip = (
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-300">
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

          <div className="rounded-xl bg-muted p-4">
            <label htmlFor="target-branch" className="text-sm font-medium">
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

      <section className="flex flex-col gap-3" aria-labelledby="briefs-heading">
        <div className="flex flex-wrap items-baseline justify-between gap-2 px-0.5">
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
    <div className="console console-cell flex min-h-32 flex-col items-start justify-center gap-4 sm:flex-row sm:items-center sm:justify-between">
      <div role="alert" className="flex items-start gap-3">
        <AlertTriangle
          aria-hidden="true"
          className="mt-0.5 size-5 shrink-0 text-destructive"
        />
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium">Your briefs are unavailable</p>
          <p className="max-w-2xl text-sm text-muted-foreground">{message}</p>
        </div>
      </div>
      <Button onClick={onRetry} className="button-ghost shrink-0">
        <RefreshCw aria-hidden="true" className="size-4" />
        Retry
      </Button>
    </div>
  );
}
