"use client";

import Header from "@/components/header";
import type { JourneyStatus, JourneySummary } from "@/types/tour";
import { Button } from "@headlessui/react";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Map,
  RefreshCw,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

function StatusBadge({ status }: { status: JourneyStatus }) {
  const map: Record<
    JourneyStatus,
    { label: string; className: string; icon: React.ReactNode }
  > = {
    complete: {
      label: "Ready",
      className: "bg-primary/15 text-primary",
      icon: <CheckCircle2 className="size-3" />,
    },
    generating: {
      label: "Generating",
      className: "bg-accent text-muted-foreground",
      icon: <Loader2 className="size-3 animate-spin" />,
    },
    pending: {
      label: "Queued",
      className: "bg-accent text-muted-foreground",
      icon: <Loader2 className="size-3 animate-spin" />,
    },
    failed: {
      label: "Failed",
      className: "bg-destructive/15 text-destructive",
      icon: <AlertTriangle className="size-3" />,
    },
  };
  const s = map[status];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${s.className}`}
    >
      {s.icon}
      {s.label}
    </span>
  );
}

export default function ToursList() {
  const [tours, setTours] = useState<JourneySummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | undefined>(undefined);

  const loadTours = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    try {
      const response = await fetch("/api/journeys", { method: "GET" });
      if (!response.ok) {
        const errBody = (await response.json().catch(() => null)) as
          | { error?: string }
          | null;
        if (response.status === 401 || response.status === 403) {
          setError(
            "Your session expired. Please refresh the page and log in again.",
          );
        } else {
          setError(errBody?.error ?? "Failed to load tours.");
        }
        return;
      }
      const result = (await response.json()) as JourneySummary[];
      setTours(Array.isArray(result) ? result : []);
    } catch (err) {
      console.log("Error: ", err);
      setError("Failed to load tours.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadTours();
  }, [loadTours]);

  return (
    <div className="flex flex-col w-full min-h-screen">
      <Header />
      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-8 pb-16">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold">Your tours</h1>
          <Button
            onClick={loadTours}
            className="flex size-8 items-center justify-center rounded-md hover:bg-accent"
            aria-label="Refresh tours"
          >
            <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />
          </Button>
        </div>

        {error && <div className="text-sm text-destructive">{error}</div>}

        {!loading && !error && tours.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-20 text-center text-muted-foreground">
            <Map className="size-8" />
            <p className="text-sm">No tours yet.</p>
            <Link
              href="/"
              className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
            >
              Generate a tour
            </Link>
          </div>
        )}

        <div className="flex flex-col gap-3">
          {tours.map((tour) => {
            const href =
              tour.status === "complete"
                ? `/tours/${tour.id}`
                : `/generate?id=${tour.id}`;
            return (
              <Link
                key={tour.id}
                href={href}
                className="flex items-center justify-between gap-4 rounded-xl border border-border p-4 transition hover:bg-accent/50"
              >
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="truncate font-medium">{tour.topic}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">
                    {tour.repoName}
                  </span>
                </div>
                <StatusBadge status={tour.status} />
              </Link>
            );
          })}
        </div>
      </div>
    </div>
  );
}
