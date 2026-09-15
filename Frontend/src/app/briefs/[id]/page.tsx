"use client";

import { Button } from "@headlessui/react";
import { useAuth } from "@clerk/nextjs";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  Loader2,
  MessageCircleQuestion,
} from "lucide-react";
import Link from "next/link";
import { use, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";

import { ApiError } from "@/lib/api";
import { cancelIssueBrief, getIssueBrief } from "@/lib/briefs";
import type { BriefArtifact, BriefResponse } from "@/types/brief";
import type { TourStep } from "@/types/tour";

const POLL_MS = 2000;

export default function BriefReader({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { getToken } = useAuth();
  const [brief, setBrief] = useState<BriefResponse>();
  const [error, setError] = useState<string>();
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const result = await getIssueBrief(id, getToken);
        if (disposed) return;
        setBrief(result);
        if (result.status === "pending" || result.status === "running" || result.status === "generating") {
          timer = setTimeout(poll, POLL_MS);
        }
      } catch (caught) {
        if (!disposed) {
          setError(caught instanceof ApiError ? caught.message : "Failed to load this brief.");
        }
      }
    }
    void poll();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [getToken, id]);

  async function stop() {
    setStopping(true);
    try {
      setBrief(await cancelIssueBrief(id, getToken));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Failed to cancel this brief.");
    } finally {
      setStopping(false);
    }
  }

  if (error) return <CenteredError message={error} />;
  if (!brief) return <CenteredLoading title="Loading brief" />;
  if (brief.status === "failed" || brief.status === "cancelled") {
    return <CenteredError message={brief.error ?? `Brief ${brief.status}.`} />;
  }
  if (brief.status !== "complete" || !brief.artifact) {
    const refreshing = brief.phase === "blocked_on_ingest";
    return (
      <div className="flex min-h-full items-center justify-center px-8">
        <div className="flex max-w-lg flex-col items-center gap-5 text-center">
          <Loader2 className="size-9 animate-spin text-primary" />
          <h1 className="text-2xl font-semibold">
            {refreshing ? "Refreshing the repository first" : "Building your issue brief"}
          </h1>
          <p className="text-sm text-muted-foreground">
            {refreshing
              ? "The code this issue touches changed since indexing — refreshing first."
              : `${brief.issueRepo} · issue #${brief.issueNumber}`}
          </p>
          <Button
            onClick={stop}
            disabled={stopping}
            className="rounded-md border border-border px-4 py-2 text-sm disabled:opacity-50"
          >
            {stopping ? "Stopping…" : "Stop generating"}
          </Button>
        </div>
      </div>
    );
  }
  return <ArtifactReader artifact={brief.artifact} />;
}

function CenteredLoading({ title }: { title: string }) {
  return (
    <div className="flex min-h-full items-center justify-center gap-3 text-muted-foreground">
      <Loader2 className="size-5 animate-spin" /> {title}
    </div>
  );
}

function CenteredError({ message }: { message: string }) {
  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-4 px-8 text-center">
      <AlertTriangle className="size-9 text-destructive" />
      <p className="text-sm text-muted-foreground">{message}</p>
      <Link href="/briefs" className="text-sm text-primary underline">Back to issue briefs</Link>
    </div>
  );
}

function ArtifactReader({ artifact }: { artifact: BriefArtifact }) {
  const followup = new URLSearchParams({
    repo: artifact.repo_name,
    question: `Follow-up about issue #${artifact.issue_number}: ${artifact.issue_title}`,
  });
  return (
    <div className="page-shell">
      <header className="console flex flex-col gap-3 p-6 sm:p-8">
        <Link href="/briefs" className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-4" /> All issue briefs
        </Link>
        <div className="font-mono text-xs text-muted-foreground">
          {artifact.repo_name} · issue #{artifact.issue_number}
        </div>
        <h1 className="display-title text-4xl font-black">{artifact.issue_title}</h1>
        <span className="w-fit rounded-full bg-accent px-2.5 py-1 text-xs capitalize text-muted-foreground">
          {artifact.confidence.level} confidence
        </span>
      </header>

      {artifact.preflight_warnings.length > 0 && (
        <div className="flex flex-col gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
          {artifact.preflight_warnings.map((warning, index) => (
            <div key={`${warning.kind}-${index}`} className="flex items-start gap-2 text-sm text-amber-300">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              <span>{warning.message}</span>
              {warning.url && (
                <a href={warning.url} target="_blank" rel="noreferrer" aria-label="Open linked pull request">
                  <ExternalLink className="size-4" />
                </a>
              )}
            </div>
          ))}
        </div>
      )}

      <Section title="1. Summary"><Markdown value={artifact.summary} /></Section>

      {artifact.confidence.issue_is_vague && artifact.confidence.questions_for_maintainer.length > 0 && (
        <Section title="Questions for the maintainer">
          <List items={artifact.confidence.questions_for_maintainer} />
        </Section>
      )}

      <Section title="2. House rules">
        <List items={artifact.house_rules} empty="No repository-specific rules were established by the evidence." />
      </Section>

      <Section title="3. Setup recipe">
        <div className="mb-4 rounded-xl bg-muted p-4 text-sm">
          <p>Target <span className="font-mono text-primary">{artifact.setup_recipe.target_branch ?? "unresolved"}</span></p>
          <p className="mt-1 text-xs text-muted-foreground">
            {artifact.setup_recipe.target_branch_evidence ?? artifact.setup_recipe.target_branch_source}
          </p>
          {artifact.setup_recipe.fork_repo && artifact.setup_recipe.fork_status_measurable && (
            <p className="mt-2 text-xs text-muted-foreground">
              {artifact.setup_recipe.fork_repo} is {artifact.setup_recipe.fork_commits_behind} commits behind upstream.
            </p>
          )}
        </div>
        <List items={artifact.setup_recipe.steps} />
      </Section>

      <Section title="4. Read the code">
        <div className="flex flex-col gap-7">
          {artifact.reading_steps.length > 0
            ? artifact.reading_steps.map((step, index) => <ReadingStep key={`${step.file_path}-${index}`} step={step} index={index} />)
            : <p className="text-sm text-muted-foreground">No grounded reading steps were available.</p>}
        </div>
      </Section>

      <Section title="5. Test guidance"><List items={artifact.test_guidance} /></Section>
      <Section title="6. Plan checklist" ><Checklist items={artifact.plan_checklist} /></Section>
      <Section title="7. Freshness"><Freshness artifact={artifact} /></Section>

      {artifact.honesty_note && (
        <footer className="flex gap-2 border-t border-border pt-6 text-sm text-amber-300">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" /> {artifact.honesty_note}
        </footer>
      )}

      <Link
        href={`/explore?${followup.toString()}`}
        className="inline-flex w-fit items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
      >
        <MessageCircleQuestion className="size-4" /> Ask a follow-up
      </Link>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="console flex flex-col gap-4 p-5 sm:p-6"><h2 className="font-mono text-sm uppercase tracking-[.14em]">{title}</h2>{children}</section>;
}

function Markdown({ value }: { value: string }) {
  return <div className="text-sm leading-relaxed [&_p:not(:last-child)]:mb-3 [&_code]:rounded [&_code]:bg-accent [&_code]:px-1"><ReactMarkdown>{value}</ReactMarkdown></div>;
}

function List({ items, empty = "No items." }: { items: string[]; empty?: string }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return <ul className="list-disc space-y-2 pl-5 text-sm">{items.map((item, index) => <li key={index}>{item}</li>)}</ul>;
}

function Checklist({ items }: { items: string[] }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">No checklist items.</p>;
  return <ul className="space-y-2 text-sm">{items.map((item, index) => <li key={index} className="flex gap-2"><CheckCircle2 className="mt-0.5 size-4 shrink-0 text-primary" />{item}</li>)}</ul>;
}

function ReadingStep({ step, index }: { step: TourStep; index: number }) {
  return (
    <article className="flex flex-col gap-3">
      <h3 className="font-medium">{index + 1}. {step.title}</h3>
      <Markdown value={step.explanation} />
      <div className="overflow-hidden rounded-xl border border-border">
        <div className="border-b border-border bg-muted px-4 py-2 font-mono text-xs text-muted-foreground">
          {step.file_path}:{step.start_line}-{step.end_line}
        </div>
        <pre className="overflow-x-auto bg-card p-4 text-xs leading-relaxed"><code>{step.snippet}</code></pre>
      </div>
      {step.why && <p className="text-sm text-muted-foreground"><span className="font-medium text-foreground">Why: </span>{step.why}</p>}
    </article>
  );
}

function Freshness({ artifact }: { artifact: BriefArtifact }) {
  const freshness = artifact.freshness;
  if (!freshness || !freshness.measurable) return <p className="text-sm text-muted-foreground">Index freshness could not be measured.</p>;
  return (
    <div className="text-sm text-muted-foreground">
      <p>Indexed at <span className="font-mono">{freshness.indexed_sha?.slice(0, 7) ?? "unknown"}</span>; head is <span className="font-mono">{freshness.head_sha?.slice(0, 7) ?? "unknown"}</span> ({freshness.commits_behind ?? 0} commits ahead).</p>
      {freshness.changed_cited_files.map((path) => <p key={path} className="mt-2 text-amber-300">⚠ {path} changed since indexing.</p>)}
    </div>
  );
}
