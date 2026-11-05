"use client";

import { Button } from "@headlessui/react";
import {
  AlertTriangle,
  ArrowUpRight,
  Loader2,
  RefreshCw,
  Square,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";

import { ApiError } from "@/lib/api";
import type { BriefResponse } from "@/types/brief";

type BriefPaneProps = {
  brief: BriefResponse | undefined;
  createdAt?: string;
  loading: boolean;
  error?: string;
  /** True when no brief is selected, e.g. after deleting the last one shown. */
  nothingSelected?: boolean;
  onCancel: (brief: BriefResponse) => Promise<void>;
  onRegenerate: (brief: BriefResponse) => Promise<void>;
  /** Rejects when the delete fails; the pane shows the error. */
  onDelete: (brief: BriefResponse) => Promise<void>;
};

type PaneAction = "cancel" | "regenerate" | "delete";
type FocusTarget = "trigger" | "keep";

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
  nothingSelected = false,
  onCancel,
  onRegenerate,
  onDelete,
}: BriefPaneProps) {
  const [action, setAction] = useState<PaneAction>();
  // Kept here, above the status branches, so an open confirmation survives
  // the brief finishing while it is open.
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string>();
  // Where focus goes when the delete controls next mount.
  const pendingFocusRef = useRef<FocusTarget | undefined>(undefined);

  async function runAction(kind: "cancel" | "regenerate") {
    if (!brief) return;
    setAction(kind);
    try {
      await (kind === "cancel" ? onCancel(brief) : onRegenerate(brief));
    } finally {
      setAction(undefined);
    }
  }

  function askToDelete() {
    setDeleteError(undefined);
    pendingFocusRef.current = "keep";
    setConfirmingDelete(true);
  }

  function keepBrief() {
    pendingFocusRef.current = "trigger";
    setConfirmingDelete(false);
    setDeleteError(undefined);
  }

  async function confirmDelete() {
    if (!brief || action !== undefined) return;
    setAction("delete");
    setDeleteError(undefined);
    try {
      // On success the workbench selects another brief and this pane unmounts.
      await onDelete(brief);
    } catch (caught) {
      setDeleteError(
        caught instanceof ApiError
          ? caught.message
          : "Failed to delete this brief. Try again.",
      );
    } finally {
      setAction(undefined);
    }
  }

  function takePendingFocus() {
    const target = pendingFocusRef.current;
    pendingFocusRef.current = undefined;
    return target;
  }

  const deleteControl = brief ? (
    confirmingDelete ? (
      <DeleteConfirm
        briefId={brief.id}
        generating={!["complete", "failed", "cancelled"].includes(brief.status)}
        deleting={action === "delete"}
        error={deleteError}
        takePendingFocus={takePendingFocus}
        onConfirm={() => void confirmDelete()}
        onKeep={keepBrief}
      />
    ) : (
      <DeleteTrigger
        disabled={action !== undefined}
        takePendingFocus={takePendingFocus}
        onClick={askToDelete}
      />
    )
  ) : null;

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

  if (!brief) {
    return nothingSelected ? (
      <PaneFrame>
        <p className="flex min-h-40 items-center justify-center px-6 text-center text-sm text-muted-foreground">
          Select a brief to preview it.
        </p>
      </PaneFrame>
    ) : null;
  }

  const terminal = ["complete", "failed", "cancelled"].includes(brief.status);
  if (!terminal) {
    const refreshing = brief.phase === "blocked_on_ingest";
    const queued = brief.phase === "queued" || brief.status === "pending";
    return (
      <PaneFrame>
        <div className="flex min-h-80 flex-col items-center justify-center gap-5 px-6 py-12 text-center">
          <Loader2 aria-hidden="true" className="size-9 animate-spin text-primary" />
          <div role="status" aria-live="polite" aria-busy="true" className="flex flex-col gap-2">
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
          {confirmingDelete ? (
            <div className="w-full max-w-lg text-left">{deleteControl}</div>
          ) : (
            <div className="flex flex-wrap justify-center gap-2.5">
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
              {deleteControl}
            </div>
          )}
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
          {confirmingDelete ? (
            <div className="w-full max-w-lg text-left">{deleteControl}</div>
          ) : (
            <div className="flex flex-wrap justify-center gap-2.5">
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
              {deleteControl}
            </div>
          )}
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

        {confirmingDelete ? (
          deleteControl
        ) : (
          <div className="flex flex-wrap items-center gap-2.5">
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
            <div className="sm:ml-auto">{deleteControl}</div>
          </div>
        )}
      </div>
    </section>
  );
}

/** True when focus fell to the page because its element was removed. */
function focusWasLost() {
  const active = document.activeElement;
  return active === null || active === document.body;
}

function DeleteTrigger({
  disabled,
  takePendingFocus,
  onClick,
}: {
  disabled: boolean;
  takePendingFocus: () => FocusTarget | undefined;
  onClick: () => void;
}) {
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // Back from the confirmation (Keep or Escape): return focus here.
    if (takePendingFocus() === "trigger") buttonRef.current?.focus();
    // Mount only: later renders must not move focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Button
      ref={buttonRef}
      onClick={onClick}
      disabled={disabled}
      className="button-ghost text-destructive hover:border-destructive"
    >
      <Trash2 aria-hidden="true" className="size-4" />
      Delete
    </Button>
  );
}

function DeleteConfirm({
  briefId,
  generating,
  deleting,
  error,
  takePendingFocus,
  onConfirm,
  onKeep,
}: {
  briefId: number;
  generating: boolean;
  deleting: boolean;
  error?: string;
  takePendingFocus: () => FocusTarget | undefined;
  onConfirm: () => void;
  onKeep: () => void;
}) {
  const keepRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const questionId = `delete-brief-question-${briefId}`;
  const detailId = `delete-brief-detail-${briefId}`;

  useEffect(() => {
    if (takePendingFocus() === "keep") {
      keepRef.current?.focus();
    } else if (focusWasLost()) {
      // The pane re-rendered a different status branch (the brief finished
      // while this was open), so the focused button was replaced.
      (deleting ? confirmRef : keepRef).current?.focus();
    }
    // Mount only: later renders must not move focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      role="group"
      aria-labelledby={questionId}
      aria-describedby={detailId}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !deleting) {
          event.stopPropagation();
          onKeep();
        }
      }}
      className="flex w-full flex-col gap-3 rounded-[8px] border border-destructive/45 bg-destructive/[.07] px-4 py-3 sm:flex-row sm:flex-wrap sm:items-center"
    >
      <div className="min-w-0 flex-1 text-sm">
        <p id={questionId} className="font-medium">
          Delete this brief?
        </p>
        <p id={detailId} className="mt-0.5 text-xs text-muted-foreground">
          {generating
            ? "It will stop generating and be removed."
            : "It disappears from your list. You can generate it again later."}
        </p>
        {error && (
          <p role="alert" className="mt-1.5 text-xs text-destructive">
            {error}
          </p>
        )}
      </div>
      <div className="flex gap-2">
        {/* aria-disabled rather than disabled while deleting, so the focused
            button keeps focus. */}
        <button
          ref={confirmRef}
          type="button"
          aria-disabled={deleting}
          onClick={() => {
            if (!deleting) onConfirm();
          }}
          className="inline-flex min-h-[35px] items-center justify-center gap-2 rounded-[4px] bg-destructive px-4 font-mono text-[10px] uppercase tracking-[.2em] text-primary-foreground transition hover:brightness-110 aria-disabled:cursor-not-allowed aria-disabled:opacity-60"
        >
          {deleting ? (
            <Loader2 aria-hidden="true" className="size-4 animate-spin" />
          ) : (
            <Trash2 aria-hidden="true" className="size-4" />
          )}
          {deleting ? "Deleting…" : "Delete brief"}
        </button>
        <button
          ref={keepRef}
          type="button"
          aria-disabled={deleting}
          onClick={() => {
            if (!deleting) onKeep();
          }}
          className="button-ghost aria-disabled:cursor-not-allowed aria-disabled:opacity-60"
        >
          Keep
        </button>
      </div>
    </div>
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
