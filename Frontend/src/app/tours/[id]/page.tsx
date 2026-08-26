"use client";

import type { JourneyResponse, TourStep } from "@/types/tour";
import { ApiError, backendFetch } from "@/lib/api";
import { useAuth } from "@clerk/nextjs";
import {
  AlertTriangle,
  ArrowLeft,
  Lightbulb,
  Loader2,
} from "lucide-react";
import Link from "next/link";
import { use, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";

function stepAnchor(index: number): string {
  return `step-${index + 1}`;
}

export default function TourReader({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { getToken } = useAuth();

  const [journey, setJourney] = useState<JourneyResponse | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(undefined);
      try {
        const token = await getToken();
        if (!token) throw new ApiError(401, "Not authenticated");

        const result = await backendFetch<JourneyResponse>(
          `/api/v1/journeys/${encodeURIComponent(id)}`,
          token,
        );
        if (cancelled) return;
        setJourney(result);
      } catch (err) {
        if (cancelled) return;
        console.log("Error: ", err);
        if (
          err instanceof ApiError &&
          (err.status === 401 || err.status === 403)
        ) {
          setError(
            "Your session expired. Please refresh the page and log in again.",
          );
        } else if (err instanceof ApiError && err.status === 404) {
          setError("We couldn't find this tour. It may have been removed.");
        } else {
          setError(
            err instanceof ApiError ? err.message : "Failed to load this tour.",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [getToken, id]);

  const artifact = journey?.artifact ?? undefined;

  const toc = useMemo(
    () => artifact?.steps.map((s, i) => ({ title: s.title, anchor: stepAnchor(i) })) ?? [],
    [artifact],
  );

  if (loading) {
    return (
      <div className="flex flex-col w-full min-h-full">
        <div className="flex flex-1 items-center justify-center">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      </div>
    );
  }

  if (error || !journey) {
    return (
      <div className="flex flex-col w-full min-h-full">
        <div className="flex flex-1 flex-col items-center justify-center gap-4 text-center px-8">
          <AlertTriangle className="size-10 text-destructive" />
          <p className="text-sm text-muted-foreground">
            {error ?? "Tour not found."}
          </p>
          <Link href="/tours" className="text-sm text-primary underline">
            Back to tours
          </Link>
        </div>
      </div>
    );
  }

  if (journey.status !== "complete" || !artifact) {
    const isFailed = journey.status === "failed";
    return (
      <div className="flex flex-col w-full min-h-full">
        <div className="flex flex-1 flex-col items-center justify-center gap-4 text-center px-8">
          {isFailed ? (
            <AlertTriangle className="size-10 text-destructive" />
          ) : (
            <Loader2 className="size-8 animate-spin text-muted-foreground" />
          )}
          <h2 className="text-xl font-semibold">
            {isFailed ? "This tour failed to generate" : "This tour is still generating"}
          </h2>
          {isFailed && journey.error && (
            <p className="text-sm text-muted-foreground">{journey.error}</p>
          )}
          {!isFailed && (
            <Link
              href={`/generate?id=${journey.id}`}
              className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
            >
              View progress
            </Link>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col w-full min-h-full">
      <div className="mx-auto flex w-full max-w-[1400px] flex-1 flex-col gap-6 px-8 py-12 lg:flex-row">
        {/* Table of contents */}
        <aside className="w-full shrink-0 lg:sticky lg:top-8 lg:h-fit lg:w-64">
          <Link
            href="/tours"
            className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-4" />
            All tours
          </Link>
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Steps
          </div>
          <nav className="mt-2 flex flex-col gap-1">
            {toc.map((item, i) => (
              <a
                key={item.anchor}
                href={`#${item.anchor}`}
                className="flex gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                <span className="text-primary">{i + 1}.</span>
                <span className="truncate">{item.title}</span>
              </a>
            ))}
          </nav>
        </aside>

        {/* Tour body */}
        <main className="flex min-w-0 flex-1 flex-col gap-10">
          <header className="flex flex-col gap-2 border-b border-border pb-6">
            <h1 className="text-3xl font-semibold">{artifact.title}</h1>
            <p className="text-sm text-muted-foreground">
              {artifact.topic} ·{" "}
              <span className="font-mono">{artifact.repo_name}</span> ·{" "}
              {artifact.steps.length} steps
            </p>
          </header>

          {artifact.steps.map((step, i) => (
            <StepBlock key={i} step={step} index={i} />
          ))}
        </main>
      </div>
    </div>
  );
}

function StepBlock({ step, index }: { step: TourStep; index: number }) {
  const startLine = step.start_line;
  const lines = step.snippet.split("\n");

  return (
    <section
      id={stepAnchor(index)}
      className="flex scroll-mt-8 flex-col gap-4"
    >
      <div className="flex items-start gap-3">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-medium text-primary-foreground">
          {index + 1}
        </span>
        <h2 className="text-xl font-semibold leading-7">{step.title}</h2>
      </div>

      <div className="text-sm leading-relaxed [&_p:not(:last-child)]:mb-3 [&_ul]:mb-3 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:mb-3 [&_ol]:list-decimal [&_ol]:pl-5 [&_li:not(:last-child)]:mb-1 [&_code]:rounded [&_code]:bg-accent [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-xs [&_strong]:font-semibold">
        <ReactMarkdown>{step.explanation}</ReactMarkdown>
      </div>

      {/* Code citation */}
      <div className="overflow-hidden rounded-xl border border-border">
        <div className="flex items-center justify-between gap-3 border-b border-border bg-muted px-4 py-2">
          <span className="truncate font-mono text-xs text-muted-foreground">
            {step.file_path}:{step.start_line}-{step.end_line}
          </span>
          {step.language && (
            <span className="shrink-0 rounded-sm bg-accent px-1.5 py-0.5 text-xs text-muted-foreground">
              {step.language}
            </span>
          )}
        </div>
        <pre className="overflow-x-auto bg-card p-4 text-xs leading-relaxed">
          <code className="font-mono">
            {lines.map((line, li) => (
              <span key={li} className="flex">
                <span className="mr-4 w-10 shrink-0 select-none text-right text-muted-foreground">
                  {startLine + li}
                </span>
                <span className="whitespace-pre">{line || " "}</span>
              </span>
            ))}
          </code>
        </pre>
      </div>

      {step.why && (
        <div className="flex gap-3 rounded-xl border border-primary/30 bg-primary/5 p-4">
          <Lightbulb className="size-4 shrink-0 text-primary" />
          <div className="text-sm leading-relaxed text-muted-foreground">
            <span className="font-medium text-foreground">Why: </span>
            {step.why}
          </div>
        </div>
      )}
    </section>
  );
}
