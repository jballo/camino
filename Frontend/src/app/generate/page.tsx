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
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

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
  const [timedOut, setTimedOut] = useState(false);
  const [pollingSession, setPollingSession] = useState(0);
  const pollingStoppedRef = useRef(false);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );

  useEffect(() => {
    if (!id) {
      router.replace("/tours");
      return;
    }

    let cancelled = false;
    pollingStoppedRef.current = false;
    const deadline = Date.now() + POLL_TIMEOUT_MS;

    const poll = async () => {
      if (pollingStoppedRef.current) return;
      if (Date.now() >= deadline) {
        pollingStoppedRef.current = true;
        setTimedOut(true);
        return;
      }

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

        const remainingMs = deadline - Date.now();
        if (remainingMs <= 0) {
          pollingStoppedRef.current = true;
          setTimedOut(true);
          return;
        }

        pollTimerRef.current = setTimeout(
          poll,
          Math.min(POLL_INTERVAL_MS, remainingMs),
        );
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
  }, [getToken, id, pollingSession, router]);

  const keepWaiting = () => {
    setTimedOut(false);
    setPollingSession((session) => session + 1);
  };

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
        <div className="console flex w-full max-w-xl flex-col gap-8 p-6 sm:p-8">
          {journey?.status === "cancelled" ? (
            <div className="flex flex-col items-center gap-4 text-center">
              <AlertTriangle className="size-10 text-muted-foreground" />
              <h2 className="text-xl font-semibold">Generation cancelled</h2>
              <p className="text-sm text-muted-foreground">
                This tour was stopped before it finished.
              </p>
              <Link
                href="/tours"
                className="button-primary"
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
                href="/tours"
                className="button-primary"
              >
                Start over
              </Link>
            </div>
          ) : timedOut ? (
            <div className="flex flex-col items-center gap-4 text-center">
              <AlertTriangle className="size-10 text-muted-foreground" />
              <h2 className="text-xl font-semibold">
                This is taking longer than expected
              </h2>
              <p className="text-sm text-muted-foreground">
                Your tour may still be generating in the background.
              </p>
              <div className="flex items-center gap-3">
                <Button
                  onClick={keepWaiting}
                  className="button-primary"
                >
                  Keep waiting
                </Button>
                <Link
                  href="/tours"
                  className="button-ghost"
                >
                  Start over
                </Link>
              </div>
            </div>
          ) : (
            <>
              <div className="flex flex-col items-center gap-2 text-center">
                <span className="eyebrow flex items-center gap-2"><span className="status-dot" /> Live process</span>
                <h1 className="display-title mt-2 text-4xl font-black">Building your tour</h1>
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
                      className="flex items-center gap-3 border-b border-border p-4 last:border-0"
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
                  className="button-ghost self-center disabled:opacity-60"
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
