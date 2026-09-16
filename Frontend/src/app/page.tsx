"use client";

import { Button, Input } from "@headlessui/react";
import { useAuth } from "@clerk/nextjs";
import { AlertTriangle, BookOpen, ExternalLink, Loader2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { ApiError } from "@/lib/api";
import {
  createIssueBrief,
  listIssueBriefs,
  previewIssueBrief,
} from "@/lib/briefs";
import type { BriefPreview, BriefSummary } from "@/types/brief";

export default function Home() {
  const { getToken } = useAuth();
  const router = useRouter();
  const [issueUrl, setIssueUrl] = useState("");
  const [preview, setPreview] = useState<BriefPreview>();
  const [branch, setBranch] = useState("");
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string>();
  const [briefs, setBriefs] = useState<BriefSummary[]>([]);

  useEffect(() => {
    void listIssueBriefs(getToken)
      .then(setBriefs)
      .catch(() => undefined);
  }, [getToken]);

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
      router.push(`/briefs/${result.id}`);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Failed to start the issue brief.",
      );
      setCreating(false);
    }
  }

  return (
    <div className="page-shell">
      <header className="flex flex-col items-center gap-3 text-center">
        <div className="flex items-center gap-3">
          <BookOpen className="size-5 text-brand-accent" />
          <span className="eyebrow">Open-source contribution helper</span>
        </div>
        <h1 className="display-title text-5xl font-black sm:text-7xl">Solve your first issue<span className="text-brand-accent">.</span></h1>
        <p className="max-w-2xl text-base text-muted-foreground">
          Paste a GitHub issue. Camino checks contribution signals, finds the
          right branch, and generates a grounded implementation brief.
        </p>
      </header>

      <section className="console flex flex-col gap-3 p-5 sm:p-6">
        <label htmlFor="issue-url" className="text-sm font-medium">
          GitHub issue URL
        </label>
        <div className="flex flex-col gap-3 sm:flex-row">
          <Input
            id="issue-url"
            type="url"
            value={issueUrl}
            onChange={(event) => setIssueUrl(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void inspectIssue();
            }}
            placeholder="https://github.com/owner/repo/issues/123"
            className="field-control min-w-0 flex-1 text-sm"
          />
          <Button
            onClick={inspectIssue}
            disabled={loading || !issueUrl.trim()}
            className="button-primary"
          >
            {loading && <Loader2 className="size-4 animate-spin" />}
            Preview
          </Button>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
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
                    <AlertTriangle className="size-3.5" />
                    {warning.message}
                    {warning.url && <ExternalLink className="size-3" />}
                  </span>
                );
                return warning.url ? (
                  <a key={`${warning.kind}-${index}`} href={warning.url} target="_blank" rel="noreferrer">
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
            {branch === preview.targetBranch.branch && preview.forkStatus.measurable && preview.forkStatus.forkRepo && (
              <p className="mt-3 text-xs text-muted-foreground">
                Your fork is {preview.forkStatus.commitsBehind} commits behind upstream/{branch}.
              </p>
            )}
          </div>

          <Button
            onClick={generate}
            disabled={creating || !branch.trim()}
            className="button-primary w-fit"
          >
            {creating && <Loader2 className="size-4 animate-spin" />}
            Generate brief
          </Button>
        </section>
      )}

      {briefs.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="font-display text-2xl font-black uppercase">Recent briefs</h2>
          <div className="border-y border-border">{briefs.map((brief, index) => (
            <Link
              key={brief.id}
              href={`/briefs/${brief.id}`}
              className="ledger-row"
            >
              <span className="font-display text-2xl text-muted-foreground">{String(index + 1).padStart(2, "0")}</span>
              <span className="min-w-0">
                <span className="block truncate font-medium">{brief.issueTitle}</span>
                <span className="block truncate font-mono text-xs text-muted-foreground">
                  {brief.issueRepo} · #{brief.issueNumber}
                </span>
              </span>
              <span className="state-pill capitalize">{brief.status}</span>
            </Link>
          ))}</div>
        </section>
      )}
    </div>
  );
}
