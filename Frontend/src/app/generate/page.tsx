"use client";

import type { JourneyResponse, JourneyStatus } from "@/types/tour";
import { ApiError, backendFetch } from "@/lib/api";
import { useAuth } from "@clerk/nextjs";
import { Button } from "@headlessui/react";
import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  Loader2,
} from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

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
    case "running":
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
  const { getToken } = useAuth();
  const id = searchParams.get("id");

  const [journey, setJourney] = useState<JourneyResponse | undefined>(undefined);
  const [error, setError] = useState<string | undefined>(undefined);
  const [cancelError, setCancelError] = useState<string | undefined>(undefined);
  const [stopping, setStopping] = useState(false);
  const pollingStoppedRef = useRef(false);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  useEffect(() => {
    if (!id) {
      router.replace("/");
      return;
    }

    let cancelled = false;
    pollingStoppedRef.current = false;

    const poll = async () => {
      if (pollingStoppedRef.current) return;
      try {
        const token = await getToken();
        if (!token) throw new ApiError(401, "Not authenticated");

        const result = await backendFetch<JourneyResponse>(
          `/api/v1/journeys/${encodeURIComponent(id)}`,
          token,
        );
        if (cancelled || pollingStoppedRef.current) return;

        setJourney(result);

        if (result.status === "complete") {
          router.push(`/tours/${id}`);
          return;
        }
        if (result.status === "failed") {
          setError(result.error ?? "Tour generation failed.");
          return;
        }
        if (result.status === "cancelled") {
          pollingStoppedRef.current = true;
          return;
        }

        pollTimerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
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
            err instanceof ApiError
              ? err.message
              : "Lost connection while checking progress.",
          );
        }
      }
    };

    poll();

    return () => {
      cancelled = true;
      pollingStoppedRef.current = true;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, [getToken, id, router]);

  const stopGenerating = async () => {
    if (!id || !journey) return;

    setStopping(true);
    setCancelError(undefined);
    try {
      const token = await getToken();
      if (!token) throw new ApiError(401, "Not authenticated");

      const result = await backendFetch<JourneyResponse>(
        `/api/v1/journeys/${encodeURIComponent(id)}/cancel`,
        token,
        { method: "POST" },
      );
      pollingStoppedRef.current = true;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
      setJourney(result);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        return;
      }
      setCancelError(
        err instanceof ApiError ? err.message : "Failed to stop generation.",
      );
    } finally {
      setStopping(false);
    }
  };

  const currentStatus = journey?.status ?? "pending";
  const currentRank = statusRank(currentStatus);
  const isActive =
    currentStatus === "pending" ||
    currentStatus === "generating" ||
    currentStatus === "running";

  return (
    <div className="flex flex-col w-full min-h-full">
      <div className="flex flex-1 flex-col items-center justify-center px-8 py-12">
        <div className="flex w-full max-w-md flex-col gap-8">
          {journey?.status === "cancelled" ? (
            <div className="flex flex-col items-center gap-4 text-center">
              <AlertTriangle className="size-10 text-muted-foreground" />
              <h2 className="text-xl font-semibold">Generation cancelled</h2>
              <p className="text-sm text-muted-foreground">
                This tour was stopped before it finished.
              </p>
              <Link
                href="/"
                className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
              >
                Start over
              </Link>
            </div>
          ) : error ? (
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
              {cancelError && (
                <p className="text-center text-sm text-destructive">
                  {cancelError}
                </p>
              )}
              {journey && isActive && (
                <Button
                  onClick={stopGenerating}
                  disabled={stopping}
                  className="self-center rounded-md border border-border px-4 py-2 text-sm disabled:opacity-60"
                >
                  {stopping ? "Stopping…" : "Stop generating"}
                </Button>
              )}
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
