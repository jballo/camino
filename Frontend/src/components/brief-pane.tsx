"use client";

import { Button } from "@headlessui/react";
import {
  AlertTriangle,
  ArrowUpRight,
  Loader2,
  RefreshCw,
  Square,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import ReactMarkdown from "react-markdown";

import type { BriefResponse } from "@/types/brief";

type BriefPaneProps = {
  brief: BriefResponse | undefined;
  createdAt?: string;
  loading: boolean;
  error?: string;
  onCancel: (brief: BriefResponse) => Promise<void>;
  onRegenerate: (brief: BriefResponse) => Promise<void>;
};

function relativeTime(value: string | undefined): string {
  if (!value) return "recently";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "recently";

  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} mo ago`;
  return `${Math.floor(months / 12)} y ago`;
}

export default function BriefPane({
  brief,
  createdAt,
  loading,
  error,
  onCancel,
  onRegenerate,
}: BriefPaneProps) {
  const [action, setAction] = useState<"cancel" | "regenerate">();

  async function runAction(kind: "cancel" | "regenerate") {
    if (!brief) return;
    setAction(kind);
    try {
      await (kind === "cancel" ? onCancel(brief) : onRegenerate(brief));
    } finally {
      setAction(undefined);
    }
  }

  if (loading && !brief) {
    return (
      <PaneFrame>
        <div
          role="status"
          aria-live="polite"
          aria-busy="true"
          className="flex min-h-80 items-center justify-center gap-3 text-sm text-muted-foreground"
        >
          <Loader2 aria-hidden="true" className="size-5 animate-spin" />
          Loading brief
        </div>
      </PaneFrame>
    );
  }

  if (error && !brief) {
    return (
      <PaneFrame>
        <div className="flex min-h-80 flex-col items-center justify-center gap-3 px-6 text-center">
          <AlertTriangle aria-hidden="true" className="size-7 text-destructive" />
          <p className="max-w-md text-sm text-destructive">{error}</p>
        </div>
      </PaneFrame>
    );
  }

  if (!brief) return null;

  const terminal = ["complete", "failed", "cancelled"].includes(brief.status);
  if (!terminal) {
    const refreshing = brief.phase === "blocked_on_ingest";
    const queued = brief.phase === "queued" || brief.status === "pending";
    return (
      <PaneFrame>
        <div
          aria-busy="true"
          className="flex min-h-80 flex-col items-center justify-center gap-5 px-6 py-12 text-center"
        >
          <Loader2 aria-hidden="true" className="size-9 animate-spin text-primary" />
          <div role="status" aria-live="polite" className="flex flex-col gap-2">
            <h2 className="text-2xl font-semibold">
              {refreshing
                ? "Refreshing the repository first"
                : queued
                  ? "Queued for generation"
                  : "Building your issue brief"}
            </h2>
            <p className="max-w-md text-sm text-muted-foreground">
              {refreshing
                ? "The code this issue touches changed since indexing — refreshing first."
                : queued
                  ? "Your brief is next in line. This pane will update automatically."
                  : `${brief.issueRepo} · issue #${brief.issueNumber}`}
            </p>
          </div>
          {error && <p className="max-w-md text-sm text-destructive">{error}</p>}
          <span className="state-pill capitalize">Phase · {brief.phase.replaceAll("_", " ")}</span>
          <Button
            onClick={() => void runAction("cancel")}
            disabled={action !== undefined}
            className="button-ghost text-muted-foreground hover:border-destructive hover:bg-transparent hover:text-destructive"
          >
            {action === "cancel" ? (
              <Loader2 aria-hidden="true" className="size-4 animate-spin" />
            ) : (
              <Square aria-hidden="true" className="size-3.5" />
            )}
            {action === "cancel" ? "Stopping…" : "Stop generating"}
          </Button>
        </div>
      </PaneFrame>
    );
  }

  if (brief.status === "failed" || brief.status === "cancelled" || !brief.artifact) {
    return (
      <PaneFrame>
        <div className="flex min-h-80 flex-col items-center justify-center gap-4 px-6 py-12 text-center">
          <AlertTriangle aria-hidden="true" className="size-8 text-destructive" />
          <div className="flex flex-col gap-2">
            <h2 className="text-xl font-semibold capitalize">Brief {brief.status}</h2>
            <p className="max-w-md text-sm text-destructive">
              {error ?? brief.error ?? `This brief was ${brief.status}.`}
            </p>
          </div>
          <Button
            onClick={() => void runAction("regenerate")}
            disabled={action !== undefined}
            className="button-ghost"
          >
            <RefreshCw
              aria-hidden="true"
              className={`size-4 ${action === "regenerate" ? "animate-spin" : ""}`}
            />
            {action === "regenerate" ? "Starting…" : "Regenerate"}
          </Button>
        </div>
      </PaneFrame>
    );
  }

  const artifact = brief.artifact;
  const previewItems = artifact.plan_checklist.slice(0, 3);
  const extraItems = artifact.plan_checklist.length - previewItems.length;
  const warning = artifact.preflight_warnings[0];

  return (
    <section className="console" aria-label="Brief preview">
      <div className="console-bar flex-wrap gap-x-3 py-3">
        <span>
          {brief.issueRepo} · #{brief.issueNumber} · <span className="text-success">⎇</span>{" "}
          {brief.ref ?? "unresolved"}
        </span>
        <span>Generated {relativeTime(createdAt)}</span>
      </div>
      <div className="flex flex-col gap-5 p-5 sm:p-6">
        <h2 className="text-2xl font-semibold leading-snug">{brief.issueTitle}</h2>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <div className="max-w-3xl text-sm leading-relaxed text-muted-foreground [&_code]:font-mono [&_code]:text-foreground [&_p:not(:last-child)]:mb-3">
          <ReactMarkdown>{artifact.summary}</ReactMarkdown>
        </div>

        <dl className="flex flex-wrap gap-x-8 gap-y-4 font-mono text-[10px] uppercase tracking-[.1em] text-muted-foreground">
          <Metric label="Reading steps" value={String(artifact.reading_steps.length).padStart(2, "0")} />
          <Metric label="Plan checklist" value={`${artifact.plan_checklist.length} items`} />
          <Metric
            label="Warnings"
            value={String(artifact.preflight_warnings.length).padStart(2, "0")}
            valueClassName={artifact.preflight_warnings.length ? "text-warning" : undefined}
          />
          <Metric label="Confidence" value={artifact.confidence.level} valueClassName="text-success" />
        </dl>

        {warning && (
          <div className="flex items-start gap-2 font-mono text-xs text-warning">
            <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
            <span>{warning.message}</span>
          </div>
        )}

        <div className="overflow-hidden rounded-[10px] border border-border" aria-label="Plan checklist preview">
          <div className="flex flex-wrap justify-between gap-2 border-b border-border px-3.5 py-2.5 font-mono text-[10px] uppercase tracking-[.12em] text-muted-foreground">
            <span>Plan checklist</span>
            <span>
              Preview · first {previewItems.length} of {artifact.plan_checklist.length}
            </span>
          </div>
          {previewItems.length ? (
            previewItems.map((item, index) => (
              <div
                key={`${index}-${item}`}
                className="flex gap-3 border-b border-border px-3.5 py-2.5 text-sm last:border-b-0"
              >
                <span className="shrink-0 font-mono text-xs text-brand-accent">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span>{item}</span>
              </div>
            ))
          ) : (
            <p className="px-3.5 py-3 text-sm text-muted-foreground">
              No checklist items were generated.
            </p>
          )}
          {extraItems > 0 && (
            <p className="border-t border-border px-3.5 py-2.5 font-mono text-[11px] text-muted-foreground">
              ＋ {extraItems} more in the full brief
            </p>
          )}
        </div>

        <div className="flex flex-wrap gap-2.5">
          <Link href={`/briefs/${brief.id}`} className="button-primary min-h-11 px-5">
            Open full brief
            <ArrowUpRight aria-hidden="true" className="size-4" />
          </Link>
          <Button
            onClick={() => void runAction("regenerate")}
            disabled={action !== undefined}
            className="button-ghost"
          >
            <RefreshCw
              aria-hidden="true"
              className={`size-4 ${action === "regenerate" ? "animate-spin" : ""}`}
            />
            {action === "regenerate" ? "Starting…" : "Regenerate"}
          </Button>
        </div>
      </div>
    </section>
  );
}

function PaneFrame({ children }: { children: React.ReactNode }) {
  return (
    <section className="console" aria-label="Brief preview">
      {children}
    </section>
  );
}

function Metric({
  label,
  value,
  valueClassName = "",
}: {
  label: string;
  value: string;
  valueClassName?: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={`mt-0.5 text-base normal-case tracking-normal text-foreground ${valueClassName}`}>
        {value}
      </dd>
    </div>
  );
}
