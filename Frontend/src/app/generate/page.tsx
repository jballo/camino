"use client";

import type { JourneyResponse, JourneyStatus } from "@/types/tour";
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  Loader2,
} from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

const POLL_INTERVAL_MS = 2000;

const STEPS: { status: JourneyStatus; label: string }[] = [
  { status: "pending", label: "Queued" },
  { status: "generating", label: "Generating tour" },
  { status: "complete", label: "Ready" },
];

function statusRank(status: JourneyStatus): number {
  switch (status) {
    case "pending":
      return 0;
    case "generating":
      return 1;
    case "complete":
      return 2;
    default:
      return -1;
  }
}

function GenerateInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const id = searchParams.get("id");

  const [journey, setJourney] = useState<JourneyResponse | undefined>(undefined);
  const [error, setError] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!id) {
      router.replace("/");
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    const poll = async () => {
      try {
        const response = await fetch(`/api/journeys/${id}`, { method: "GET" });
        if (!response.ok) {
          if (cancelled) return;
          const errBody = (await response.json().catch(() => null)) as
            | { error?: string }
            | null;
          if (response.status === 401 || response.status === 403) {
            setError(
              "Your session expired. Please refresh the page and log in again.",
            );
          } else if (response.status === 404) {
            setError("We couldn't find this tour. It may have been removed.");
          } else {
            setError(errBody?.error ?? "Failed to fetch journey status.");
          }
          return;
        }
        const result = (await response.json()) as JourneyResponse;
        if (cancelled) return;

        setJourney(result);

        if (result.status === "complete") {
          router.push(`/tours/${id}`);
          return;
        }
        if (result.status === "failed") {
          setError(result.error ?? "Tour generation failed.");
          return;
        }

        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch (err) {
        if (cancelled) return;
        console.log("Error: ", err);
        setError("Lost connection while checking progress.");
      }
    };

    poll();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [id, router]);

  const currentStatus = journey?.status ?? "pending";
  const currentRank = statusRank(currentStatus);

  return (
    <div className="flex flex-col w-full min-h-full">
      <div className="flex flex-1 flex-col items-center justify-center px-8 py-12">
        <div className="flex w-full max-w-md flex-col gap-8">
          {error ? (
            <div className="flex flex-col items-center gap-4 text-center">
              <AlertTriangle className="size-10 text-destructive" />
              <h2 className="text-xl font-semibold">Generation failed</h2>
              <p className="text-sm text-muted-foreground">{error}</p>
              <Link
                href="/"
                className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
              >
                Start over
              </Link>
            </div>
          ) : (
            <>
              <div className="flex flex-col items-center gap-2 text-center">
                <h2 className="text-2xl font-semibold">Building your tour</h2>
                {journey && (
                  <p className="text-sm text-muted-foreground">
                    <span className="text-foreground">{journey.topic}</span> ·{" "}
                    {journey.repoName}
                  </p>
                )}
              </div>

              <div className="flex flex-col gap-3">
                {STEPS.map((step) => {
                  const rank = statusRank(step.status);
                  const done = currentRank > rank;
                  const active = currentRank === rank;
                  return (
                    <div
                      key={step.status}
                      className="flex items-center gap-3 rounded-lg border border-border p-3"
                    >
                      {done ? (
                        <CheckCircle2 className="size-5 text-primary" />
                      ) : active ? (
                        <Loader2 className="size-5 animate-spin text-primary" />
                      ) : (
                        <Circle className="size-5 text-muted-foreground" />
                      )}
                      <span
                        className={
                          done || active
                            ? "text-sm"
                            : "text-sm text-muted-foreground"
                        }
                      >
                        {step.label}
                      </span>
                    </div>
                  );
                })}
              </div>

              <p className="text-center text-xs text-muted-foreground">
                This can take a minute. Keep this tab open.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default function Generate() {
  return (
    <Suspense
      fallback={
        <div className="flex flex-col w-full min-h-full">
          <div className="flex flex-1 items-center justify-center">
            <Loader2 className="size-6 animate-spin text-muted-foreground" />
          </div>
        </div>
      }
    >
      <GenerateInner />
    </Suspense>
  );
}
